"""T4 classification: category, urgency and confidence for incoming reports.

Design (see docs/t4-classification-spec.md):

- ``classify`` asks Groq a choice-type urgency question and expects strict JSON.
- Any failure (no key, timeout, bad JSON after one retry) degrades to keyword
  mode. Intake must NEVER block on the classifier, so this module is written to
  always return a result rather than raise.
- A module-level safety floor RAISES urgency on safety terms and never lowers
  it, in every mode.

No secrets are ever logged; ``GROQ_API_KEY`` is read at call time from the env.
"""
import json
import os

import httpx

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"

VALID_CATEGORIES = ("roads", "water", "waste", "public_services", "other")

# Urgency choice -> integer stored in reports.urgency (CHECK 1-5).
# "low" spans 1-2 in the spec; we simplify by mapping it to 2. Trivial/cosmetic
# cases still land in review (keyword/low-confidence), so no information is lost.
URGENCY_CHOICES = {"critical": 5, "high": 4, "medium": 3, "low": 2}

# Safety floor: a module constant with an env override. The floor only ever
# raises urgency when a safety term is present.
SAFETY_URGENCY_FLOOR = int(os.environ.get("CLASSIFIER_SAFETY_FLOOR", "5"))

# Confidence below this floor forces human review (env override, default 0.60).
CLASSIFICATION_CONFIDENCE_FLOOR = float(
    os.environ.get("CLASSIFICATION_CONFIDENCE_FLOOR", "0.60")
)

# Safety terms whose presence raises urgency to at least the safety floor.
# English seeds plus approximate standard Luganda and Swahili forms (these are
# best-effort spellings of the common words; they are NOT exhaustive).
SAFETY_TERMS = [
    # English
    "fire",
    "flood",
    "burst",
    "sewage",
    "collapsed",
    "electrocution",
    "gas leak",
    "death",
    "danger",
    "outbreak",
    # Luganda (approximate standard forms)
    "omuliro",      # fire
    "amataba",      # flood / floodwaters
    "okusaanuuka",  # burst / split open
    "okufa",        # death
    "akabi",        # danger
    "gaasi",        # gas
    # Swahili (approximate standard forms)
    "moto",      # fire
    "mafuriko",  # flood
    "kupasuka",  # burst / split open
    "kifo",      # death
    "hatari",    # danger
    "gesi",      # gas
    "mlipuko",   # outbreak / explosion
]

# Keyword category seeds for degraded (no-model) mode. Longer/more specific
# terms first so that, e.g., "sewage" beats a generic "water".
KEYWORD_CATEGORIES = {
    "roads": [
        "pothole", "road", "street", "tarmac", "bridge", "culvert", "drainage",
        "pavement", "sidewalk", "oluguudo",
    ],
    "water": [
        "sewage", "sewer", "water", "pipe", "tap", "borehole", "well", "leak",
        "supply", "maji", "amazzi",
    ],
    "waste": [
        "garbage", "rubbish", "trash", "waste", "bin", "dump", "litter",
        "collection", "sanitation", "kasasiro", "taka",
    ],
    "public_services": [
        "streetlight", "street light", "lamp", "power", "electric", "umeme",
        "security", "clinic", "hospitali", "hospital", "school", "shule",
        "market", "soko", "council",
    ],
}


def _timeout() -> float:
    try:
        return float(os.environ.get("CLASSIFIER_TIMEOUT", "5"))
    except (TypeError, ValueError):
        return 5.0


def _model() -> str:
    return os.environ.get("GROQ_MODEL") or DEFAULT_GROQ_MODEL


def _safety_floor() -> int:
    try:
        return int(os.environ.get("CLASSIFIER_SAFETY_FLOOR", SAFETY_URGENCY_FLOOR))
    except (TypeError, ValueError):
        return SAFETY_URGENCY_FLOOR


def confidence_floor() -> float:
    try:
        return float(
            os.environ.get(
                "CLASSIFICATION_CONFIDENCE_FLOOR", CLASSIFICATION_CONFIDENCE_FLOOR
            )
        )
    except (TypeError, ValueError):
        return CLASSIFICATION_CONFIDENCE_FLOOR


def _normalize(title: str, description: str) -> str:
    return f"{title or ''} {description or ''}".lower()


def _safety_hit(text: str) -> bool:
    return any(term in text for term in SAFETY_TERMS)


def _keyword_category(text: str) -> str:
    best_cat = "other"
    best_score = 0
    for cat, terms in KEYWORD_CATEGORIES.items():
        score = sum(1 for term in terms if term in text)
        if score > best_score:
            best_cat, best_score = cat, score
    return best_cat


def _urgency_name(value: int) -> str:
    for name, val in URGENCY_CHOICES.items():
        if val == value:
            return name
    return "low"


def _format_reason(mode: str, category: str, confidence: float, urgency: int,
                   floor_hit: bool) -> str:
    return (
        f"mode={mode}; suggested={category}; conf={confidence:.2f}; "
        f"urgency={_urgency_name(urgency)}({urgency}); "
        f"floor_hit={1 if floor_hit else 0}"
    )


def keyword_classify(title: str, description: str) -> dict:
    """Fail-closed classification: category from keyword seeds, confidence 0.0.

    Always flagged for review; safety terms raise urgency to the floor.
    """
    text = _normalize(title, description)
    safety_hit = _safety_hit(text)
    floor = _safety_floor()
    urgency = 3
    floor_hit = False
    if safety_hit and urgency < floor:
        urgency = floor
        floor_hit = True
    return {
        "suggested_category": _keyword_category(text),
        "urgency": int(urgency),
        "confidence": 0.0,
        "reason": _format_reason("keyword", _keyword_category(text), 0.0, urgency,
                                 floor_hit),
        "mode": "keyword",
        "flagged_for_review": True,
    }


_SYSTEM_PROMPT = (
    "You triage citizen civic reports. Reply with ONLY a strict JSON object and "
    "no prose: "
    '{"category": one of roads|water|waste|public_services|other, '
    '"urgency": one of critical|high|medium|low, '
    '"confidence": a number between 0.0 and 1.0}. '
    "Decision question for urgency: How urgent is this civic report? "
    "critical = immediate danger to life, health, property or safety; spreading "
    "harm; a service completely down with people at risk. "
    "high = essential service fully down for many people; unsafe conditions "
    "developing; needs action within days. "
    "medium = degraded service or a recurring nuisance; affects some people; can "
    "wait for scheduled work. "
    "low = cosmetic, minor, or already being handled; no safety impact."
)


def _user_prompt(title: str, description: str) -> str:
    return f"Title: {title or ''}\nDescription: {description or ''}"


def _parse_model_content(content: str):
    data = json.loads(content)
    category = str(data["category"]).strip().lower()
    if category not in VALID_CATEGORIES:
        raise ValueError(f"invalid category: {category!r}")
    choice = str(data["urgency"]).strip().lower()
    if choice not in URGENCY_CHOICES:
        raise ValueError(f"invalid urgency choice: {choice!r}")
    confidence = float(data["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of range: {confidence!r}")
    return category, choice, URGENCY_CHOICES[choice], confidence


def _ai_classify(title: str, description: str, key: str):
    """Call Groq; one retry on malformed JSON, then raise to fail closed."""
    payload = {
        "model": _model(),
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(title, description)},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    last_error = None
    for _ in range(2):  # one retry on malformed JSON
        response = httpx.post(
            GROQ_ENDPOINT, headers=headers, json=payload, timeout=_timeout()
        )
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        try:
            return _parse_model_content(content)
        except Exception as exc:  # malformed -> retry once
            last_error = exc
    raise ValueError(f"malformed model JSON after retry: {last_error}")


def classify(title: str, description: str) -> dict:
    """Return classification for a report; never raises (intake must not block).

    Keys: suggested_category, urgency (1-5), confidence (0-1), reason, mode
    ("ai"|"keyword"), flagged_for_review.
    """
    text = _normalize(title, description)
    safety_hit = _safety_hit(text)
    floor = _safety_floor()
    key = os.environ.get("GROQ_API_KEY")

    if key:
        try:
            category, choice, urgency, confidence = _ai_classify(
                title, description, key
            )
            floor_hit = bool(safety_hit and urgency < floor)
            if safety_hit:
                urgency = max(urgency, floor)
            return {
                "suggested_category": category,
                "urgency": int(urgency),
                "confidence": float(confidence),
                "reason": _format_reason(
                    "ai", category, confidence, urgency, floor_hit
                ),
                "mode": "ai",
                "flagged_for_review": confidence < confidence_floor(),
            }
        except Exception:
            # Any provider failure -> fail closed to keyword mode.
            pass

    return keyword_classify(title, description)
