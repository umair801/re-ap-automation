# agents/exception_handler_agent.py

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from openai import OpenAI

from core.config import get_settings
from core.logger import get_logger
from core.models import (
    ExceptionRecord,
    ExceptionType,
    Invoice,
    InvoiceStatus,
    MatchStatus,
)

logger = get_logger(__name__)
settings = get_settings()

client = OpenAI(api_key=settings.openai_api_key)


# ─── Exception Type Resolver ──────────────────────────────────────────────────

def _resolve_exception_type(invoice: Invoice) -> ExceptionType:
    """
    Determine the primary exception type from the invoice state.
    Uses validation errors and match status to classify.
    """
    if invoice.match_status == MatchStatus.PO_NOT_FOUND:
        return ExceptionType.MISSING_PO

    if invoice.match_status in (MatchStatus.MISMATCH, MatchStatus.PARTIAL_MATCH):
        return ExceptionType.MATCH_FAILED

    if invoice.validation_errors:
        error_codes = {e.error_code for e in invoice.validation_errors}

        if "DUPLICATE_INVOICE" in error_codes:
            return ExceptionType.DUPLICATE_INVOICE

        if "MISSING_REQUIRED_FIELD" in error_codes or "EMPTY_VENDOR_NAME" in error_codes:
            return ExceptionType.VALIDATION_FAILED

        if "AMOUNT_MISMATCH" in error_codes or "INVALID_AMOUNT" in error_codes:
            return ExceptionType.AMOUNT_TOLERANCE_EXCEEDED

        if "LOW_EXTRACTION_CONFIDENCE" in error_codes:
            return ExceptionType.EXTRACTION_FAILED

    return ExceptionType.VALIDATION_FAILED


def _build_exception_description(invoice: Invoice) -> str:
    """
    Build a human-readable exception description from invoice state.
    Used in the exception record and as context for GPT-4o.
    """
    lines = []

    if invoice.validation_errors:
        lines.append("Validation errors:")
        for err in invoice.validation_errors:
            lines.append(f"  - [{err.error_code}] {err.field}: {err.message}")

    if invoice.match_status:
        lines.append(f"Match status: {invoice.match_status.value}")

    if invoice.match_details:
        failed = [d for d in invoice.match_details if not d.within_tolerance]
        if failed:
            lines.append("Failed match fields:")
            for d in failed:
                lines.append(
                    f"  - {d.field}: invoice={d.invoice_value} "
                    f"vs PO={d.po_value} "
                    f"(variance: {d.variance_percent}%)"
                )

    return "\n".join(lines) if lines else "Unspecified exception."


# ─── Vendor Communication Drafter ─────────────────────────────────────────────

VENDOR_EMAIL_PROMPT = """
You are an Accounts Payable specialist at a large enterprise company.

Draft a professional, concise email to a vendor requesting correction or 
clarification on an invoice that could not be processed automatically.

Invoice details:
- Invoice Number: {invoice_number}
- Vendor: {vendor_name}
- Amount: {currency} {total}
- Due Date: {due_date}

Issues found:
{issues}

Requirements for the email:
- Professional and courteous tone
- State clearly what information or correction is needed
- Do not assign blame — frame it as a routine verification step
- Request the vendor resubmit or respond within 5 business days
- Sign off as: AP Processing Team

Return only the email body. No subject line. No extra commentary.
"""


def _draft_vendor_email(invoice: Invoice, issues: str) -> str:
    """
    Use GPT-4o to draft a professional vendor communication
    requesting correction or clarification.
    """
    vendor_name = invoice.vendor.vendor_name if invoice.vendor else "Vendor"

    prompt = VENDOR_EMAIL_PROMPT.format(
        invoice_number=invoice.invoice_number or "N/A",
        vendor_name=vendor_name,
        currency=invoice.currency,
        total=invoice.total or "N/A",
        due_date=invoice.due_date or "N/A",
        issues=issues,
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
        )
        email_body = response.choices[0].message.content.strip()
        logger.info(
            "Vendor email drafted",
            invoice_id=str(invoice.id),
            vendor=vendor_name,
        )
        return email_body

    except Exception as e:
        logger.error("Failed to draft vendor email", error=str(e))
        return (
            f"Dear {vendor_name},\n\n"
            f"We were unable to process invoice {invoice.invoice_number} "
            f"due to the following issues:\n\n{issues}\n\n"
            "Please review and resubmit within 5 business days.\n\n"
            "AP Processing Team"
        )


# ─── Vendor Notification Sender ───────────────────────────────────────────────

def _send_vendor_notification(
    invoice: Invoice,
    exception: ExceptionRecord,
    email_body: str,
) -> bool:
    """
    Send the drafted email to the vendor via SendGrid.
    Returns True on success, False if SendGrid is not configured.
    """
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        if not settings.sendgrid_api_key:
            logger.warning("SendGrid not configured — skipping vendor notification")
            return False

        vendor_email = invoice.vendor.vendor_email if invoice.vendor else None
        if not vendor_email:
            logger.warning(
                "No vendor email on file — cannot send notification",
                invoice_id=str(invoice.id),
            )
            return False

        subject = (
            f"Action Required: Invoice {invoice.invoice_number} "
            f"Requires Correction"
        )

        message = Mail(
            from_email=settings.sendgrid_from_email,
            to_emails=vendor_email,
            subject=subject,
            plain_text_content=email_body,
        )

        sg = SendGridAPIClient(settings.sendgrid_api_key)
        sg.send(message)

        logger.info(
            "Vendor notification sent",
            invoice_id=str(invoice.id),
            vendor_email=vendor_email,
        )
        return True

    except Exception as e:
        logger.error(
            "Failed to send vendor notification",
            invoice_id=str(invoice.id),
            error=str(e),
        )
        return False


# ─── Main Agent ───────────────────────────────────────────────────────────────

def run_exception_handler_agent(
    invoice: Invoice,
    notify_vendor: bool = True,
) -> tuple[Invoice, ExceptionRecord]:
    """
    Main entry point for the Exception Handler Agent.

    Creates an ExceptionRecord, drafts a vendor communication,
    optionally sends it, and marks the invoice for human review.

    Args:
        invoice: Invoice that failed validation or three-way match.
        notify_vendor: Whether to send the vendor an email notification.
                       Set to False in testing or when vendor email is absent.

    Returns:
        Tuple of (updated Invoice, ExceptionRecord).
    """
    logger.info(
        "Exception handler started",
        invoice_id=str(invoice.id),
        invoice_number=invoice.invoice_number,
        current_status=invoice.status.value,
    )

    exception_type = _resolve_exception_type(invoice)
    description = _build_exception_description(invoice)

    exception = ExceptionRecord(
        id=uuid4(),
        invoice_id=invoice.id,
        exception_type=exception_type,
        description=description,
    )

    # Draft vendor communication
    email_body = _draft_vendor_email(invoice, description)

    # Optionally send vendor notification
    if notify_vendor:
        sent = _send_vendor_notification(invoice, exception, email_body)
        exception.vendor_notified = sent
        if sent:
            exception.vendor_notification_sent_at = datetime.utcnow()
    else:
        logger.info(
            "Vendor notification skipped",
            invoice_id=str(invoice.id),
        )

    # Mark invoice as exception for human review queue
    invoice.status = InvoiceStatus.EXCEPTION

    logger.warning(
        "Invoice queued for human review",
        invoice_id=str(invoice.id),
        invoice_number=invoice.invoice_number,
        exception_type=exception_type.value,
        vendor_notified=exception.vendor_notified,
    )

    return invoice, exception