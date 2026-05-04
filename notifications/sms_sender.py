# notifications/sms_sender.py

import structlog
from core.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()


def send_sms(to_number: str, message: str) -> None:
    """Send an SMS notification via Twilio."""
    try:
        from twilio.rest import Client

        client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        msg = client.messages.create(
            body=message,
            from_=settings.TWILIO_FROM_NUMBER,
            to=to_number,
        )
        logger.info("sms_sent", to=to_number, sid=msg.sid)
    except Exception as exc:
        logger.error("sms_send_failed", to=to_number, error=str(exc))
        raise