"""Telegram intake bot: long-polling only, posts reports to the API.

No webhooks, no new ports, no new dependencies (httpx is already used by the
API). Start it with a token once one exists:

    TELEGRAM_BOT_TOKEN=... python -m app.telegram_bot
"""
import os
import sys
import time

import httpx

API_BASE = os.environ.get("API_BASE", "http://localhost:8000").rstrip("/")
CATEGORIES = ("roads", "water", "waste", "public_services", "other")

INSTRUCTIONS = (
    "Civic Issue Triage bot.\n"
    "Pick a category below, then describe the issue in your next message "
    "and I will queue it for the right authority."
)

# chat_id -> {"stage": 0|1, "category": str | None}
STATE: dict[int, dict] = {}


def _safe(exc, token):
    """Exception text with the token masked (never log the token)."""
    return str(exc).replace(token, "***")


def _keyboard():
    return {
        "inline_keyboard": [
            [{"text": c.replace("_", " ").title(), "callback_data": c}]
            for c in CATEGORIES
        ]
    }


def _send(token, client, chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        client.post(
            f"https://api.telegram.org/bot{token}/sendMessage", json=payload
        )
    except httpx.HTTPError as exc:
        print(f"sendMessage failed: {_safe(exc, token)}", file=sys.stderr)


def _reporter(msg):
    user = msg.get("from") or {}
    if user.get("username"):
        return "@" + user["username"]
    return user.get("first_name")


def _reply_instructions(token, client, chat_id):
    _send(token, client, chat_id, INSTRUCTIONS, _keyboard())


def _handle_callback(token, client, cb):
    chat_id = cb["message"]["chat"]["id"]
    category = cb["data"]
    STATE[chat_id] = {"stage": 1, "category": category}
    _send(
        token, client, chat_id,
        f"Category: {category}. Now describe the issue in one message.",
    )


def _handle_text(token, client, msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    state = STATE.get(chat_id, {"stage": 0, "category": None})

    if text in ("/start", "/help"):
        STATE[chat_id] = {"stage": 0, "category": None}
        _reply_instructions(token, client, chat_id)
        return

    if state["stage"] == 1 and state.get("category"):
        _file_report(
            token, client, chat_id, text, state["category"], _reporter(msg)
        )
        STATE[chat_id] = {"stage": 0, "category": None}
        return

    _reply_instructions(token, client, chat_id)


def _file_report(token, client, chat_id, text, category, reporter):
    title = text.strip()[:80]
    try:
        r = client.post(
            f"{API_BASE}/api/reports",
            json={
                "title": title,
                "description": text,
                "category": category,
                "source": "telegram",
                "reporter": reporter,
            },
        )
        if r.status_code == 201:
            rid = r.json().get("id")
            _send(
                token, client, chat_id,
                f"Thanks - report #{rid} is queued for routing.",
            )
        else:
            _send(
                token, client, chat_id,
                "Sorry, something went wrong. Please try again later.",
            )
    except (httpx.HTTPError, ValueError) as exc:
        print(f"report POST failed: {_safe(exc, token)}", file=sys.stderr)
        _send(
            token, client, chat_id,
            "Sorry, something went wrong. Please try again later.",
        )


def _dispatch(token, client, update):
    if "callback_query" in update:
        _handle_callback(token, client, update["callback_query"])
    elif "message" in update:
        _handle_text(token, client, update["message"])


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    offset = None
    with httpx.Client(timeout=40) as client:
        print(f"Telegram bot polling {API_BASE}; Ctrl-C to stop.")
        while True:
            try:
                params = {"timeout": 30}
                if offset is not None:
                    params["offset"] = offset
                r = client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params=params,
                )
                r.raise_for_status()
                updates = r.json().get("result", [])
            except (httpx.HTTPError, ValueError) as exc:
                print(
                    f"getUpdates failed: {_safe(exc, token)}; retrying in 3s",
                    file=sys.stderr,
                )
                time.sleep(3)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                try:
                    _dispatch(token, client, update)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"update {update.get('update_id')} failed:"
                        f" {_safe(exc, token)}",
                        file=sys.stderr,
                    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
