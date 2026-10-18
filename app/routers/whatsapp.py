"""WhatsApp webhook: Meta Cloud API events into the existing intake.

Scope ruling (Steph, T-WA lane): NO new pipeline. The POST handler extracts
the message body from each message event and feeds it through the same
`create_report` flow the web form uses (classify path included), recording
`source=whatsapp` so origin stays traceable. Messages carry no category, so
they land as `other` and ride the existing T9 review path (never auto-routed)
— that IS the fail-closed doctrine.

Non-message events (delivery/status receipts) are logged and dropped.

Meta must get its 200 fast; processing happens after the response
(BackgroundTasks). No new schema beyond migration 0003 (source CHECK).
"""
import hashlib
import hmac
import logging
import os

from fastapi import APIRouter, BackgroundTasks, Request, Response

from app.routers.reports import ReportIn, create_report

router = APIRouter()
logger = logging.getLogger("whatsapp.webhook")

VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")


class _Ingest(ReportIn):
    """Intake payload pinned to the WhatsApp source."""


def _make_ingest(title: str, description: str, reporter: str) -> dict:
    return {
        "title": title,
        "description": description,
        "category": "other",  # no category over WhatsApp → T9 review path
        "reporter": reporter,
        "source": "whatsapp",
    }


@router.get("/webhook")
def verify(request: Request):
    """Meta verification handshake: echo hub.challenge iff token matches."""
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(content="", status_code=403)


@router.post("/webhook")
async def receive(request: Request, background: BackgroundTasks):
    """Accept Meta events; reply 200 immediately, process after."""
    body = await request.body()
    if APP_SECRET:
        if not _signature_ok(request, body):
            return Response(content="invalid signature", status_code=403)
    else:
        logger.warning("WHATSAPP_APP_SECRET unset — POST signature not verified")

    try:
        payload = await request.json()
    except Exception:
        return {"received": True}  # malformed but acked; Meta retries on 4xx/5xx

    for message in _extract_messages(payload):
        background.add_task(_intake_message, message)
    return {"received": True}


def _signature_ok(request: Request, body: bytes) -> bool:
    header = request.headers.get("x-hub-signature-256", "")
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


def _extract_messages(payload: dict) -> list[dict]:
    """Pull (sender, text) pairs out of a Meta webhook payload.

    Non-message events (statuses / delivery receipts) and non-text messages
    are logged and dropped, per lane ruling.
    """
    kept: list[dict] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            dropped = value.get("statuses")
            if dropped:
                logger.info("whatsapp status receipt dropped: %d event(s)", len(dropped))
            for message in value.get("messages", []):
                text = (message.get("text") or {}).get("body", "")
                sender = message.get("from", "")
                if not text.strip():
                    logger.info("whatsapp non-text message dropped (type=%s)",
                                message.get("type"))
                    continue
                kept.append({"sender": sender, "text": text})
    return kept


def _intake_message(message: dict) -> None:
    """Feed one WhatsApp message through the SAME intake as the web form."""
    text = message["text"].strip()
    title = text.splitlines()[0][:200].strip() or text[:200]
    try:
        record = create_report(_Ingest(**_make_ingest(
            title=title, description=text, reporter=message["sender"]
        )))
        logger.info("whatsapp intake: report id=%s from=%s",
                    record.get("id"), message["sender"])
    except Exception:
        # Intake must never crash the worker or ack-path; failures surface in
        # the logs and Meta-side silence, same doctrine as the classifier.
        logger.exception("whatsapp intake failed for sender=%s", message["sender"])
