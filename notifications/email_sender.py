# notifications/email_sender.py

import structlog
from core.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()


def send_email(to_email: str, subject: str, body: str) -> None:
    """Send an email notification via SendGrid."""
    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail

        sg = sendgrid.SendGridAPIClient(api_key=settings.SENDGRID_API_KEY)
        message = Mail(
            from_email=settings.SENDGRID_FROM_EMAIL,
            to_emails=to_email,
            subject=subject,
            plain_text_content=body,
        )
        response = sg.send(message)
        logger.info(
            "email_sent",
            to=to_email,
            subject=subject,
            status=response.status_code,
        )
    except Exception as exc:
        logger.error("email_send_failed", to=to_email, error=str(exc))
        raise