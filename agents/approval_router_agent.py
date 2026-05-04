# agents/approval_router_agent.py

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from core.config import get_settings
from core.logger import get_logger
from core.models import (
    ApprovalRecord,
    ApprovalStatus,
    Invoice,
    InvoiceStatus,
)

logger = get_logger(__name__)
settings = get_settings()


# ─── Approval Matrix ──────────────────────────────────────────────────────────

# Configurable per client in production — loaded from Supabase or config file.
# Format: list of tiers in ascending order.
# Each tier: (max_amount, approver_name, approver_email, approver_phone)
DEFAULT_APPROVAL_MATRIX = [
    {
        "max_amount": Decimal("1000.00"),
        "approver_name": "Auto Approve",
        "approver_email": "",
        "approver_phone": "",
        "auto_approve": True,
    },
    {
        "max_amount": Decimal("5000.00"),
        "approver_name": "AP Manager",
        "approver_email": "ap.manager@company.com",
        "approver_phone": "+1234567890",
        "auto_approve": False,
    },
    {
        "max_amount": Decimal("25000.00"),
        "approver_name": "Finance Director",
        "approver_email": "finance.director@company.com",
        "approver_phone": "+1234567891",
        "auto_approve": False,
    },
    {
        "max_amount": Decimal("999999999.00"),
        "approver_name": "CFO",
        "approver_email": "cfo@company.com",
        "approver_phone": "+1234567892",
        "auto_approve": False,
    },
]


def _get_approval_tier(
    amount: Decimal,
    matrix: Optional[list[dict]] = None,
) -> dict:
    """
    Find the correct approval tier for a given invoice amount.
    Returns the first tier whose max_amount >= the invoice amount.
    """
    tiers = matrix or DEFAULT_APPROVAL_MATRIX

    for tier in sorted(tiers, key=lambda t: t["max_amount"]):
        if amount <= tier["max_amount"]:
            return tier

    # Fallback to highest tier
    return tiers[-1]


def _generate_approval_token() -> str:
    """Generate a cryptographically secure token for approve/reject links."""
    return secrets.token_urlsafe(32)


def _build_approval_url(base_url: str, token: str, decision: str) -> str:
    """
    Build a one-click approval or rejection URL.
    In production this points to the FastAPI approval endpoint.
    """
    return f"{base_url}/api/approval/{decision}?token={token}"


# ─── Notification Helpers ─────────────────────────────────────────────────────

def _send_approval_email(
    approval: ApprovalRecord,
    invoice: Invoice,
    approve_url: str,
    reject_url: str,
) -> bool:
    """
    Send approval request email to the designated approver.
    Uses SendGrid in production. Logs and returns False if unavailable.
    """
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        if not settings.sendgrid_api_key:
            logger.warning("SendGrid not configured — skipping email notification")
            return False

        vendor_name = invoice.vendor.vendor_name if invoice.vendor else "Unknown Vendor"

        subject = (
            f"Approval Required: Invoice {invoice.invoice_number} "
            f"from {vendor_name} for {invoice.currency} {invoice.total}"
        )

        body = f"""
<h2>Invoice Approval Request</h2>
<p>An invoice requires your approval before payment can be processed.</p>

<table style="border-collapse: collapse; width: 100%;">
  <tr><td><strong>Invoice Number</strong></td><td>{invoice.invoice_number}</td></tr>
  <tr><td><strong>Vendor</strong></td><td>{vendor_name}</td></tr>
  <tr><td><strong>Amount</strong></td><td>{invoice.currency} {invoice.total}</td></tr>
  <tr><td><strong>Due Date</strong></td><td>{invoice.due_date}</td></tr>
  <tr><td><strong>PO Number</strong></td><td>{invoice.po_number or 'N/A'}</td></tr>
</table>

<br/>
<a href="{approve_url}"
   style="background:#22c55e;color:white;padding:12px 24px;
          text-decoration:none;border-radius:6px;margin-right:12px;">
   APPROVE
</a>
<a href="{reject_url}"
   style="background:#ef4444;color:white;padding:12px 24px;
          text-decoration:none;border-radius:6px;">
   REJECT
</a>

<p style="color:#6b7280;font-size:12px;margin-top:24px;">
This approval link expires in {settings.approval_timeout_hours} hours.
</p>
        """

        message = Mail(
            from_email=settings.sendgrid_from_email,
            to_emails=approval.approver_email,
            subject=subject,
            html_content=body,
        )

        sg = SendGridAPIClient(settings.sendgrid_api_key)
        sg.send(message)

        logger.info(
            "Approval email sent",
            invoice_id=str(invoice.id),
            approver=approval.approver_email,
        )
        return True

    except Exception as e:
        logger.error("Failed to send approval email", error=str(e))
        return False


def _send_approval_sms(
    approval: ApprovalRecord,
    invoice: Invoice,
    approve_url: str,
) -> bool:
    """
    Send a brief SMS notification to the approver.
    Uses Twilio in production.
    """
    try:
        from twilio.rest import Client

        if not settings.twilio_account_sid or not approval.approver_phone:
            logger.warning("Twilio not configured — skipping SMS notification")
            return False

        vendor_name = invoice.vendor.vendor_name if invoice.vendor else "Unknown"

        body = (
            f"AP Approval Needed: Invoice {invoice.invoice_number} "
            f"from {vendor_name} for {invoice.currency} {invoice.total}. "
            f"Approve: {approve_url}"
        )

        twilio_client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        twilio_client.messages.create(
            body=body,
            from_=settings.twilio_from_number,
            to=approval.approver_phone,
        )

        logger.info(
            "Approval SMS sent",
            invoice_id=str(invoice.id),
            approver_phone=approval.approver_phone,
        )
        return True

    except Exception as e:
        logger.error("Failed to send approval SMS", error=str(e))
        return False


# ─── Main Agent ───────────────────────────────────────────────────────────────

def run_approval_router_agent(
    invoice: Invoice,
    approval_matrix: Optional[list[dict]] = None,
    base_url: str = "https://ap.datawebify.com",
) -> tuple[Invoice, ApprovalRecord]:
    """
    Main entry point for the Approval Router Agent.

    Determines the correct approval tier for the invoice amount.
    Auto-approves if below threshold, or creates an ApprovalRecord
    and sends email/SMS notification to the designated approver.

    Args:
        invoice: Matched and validated Invoice model.
        approval_matrix: Optional custom matrix. Uses DEFAULT_APPROVAL_MATRIX
                         if not provided.
        base_url: Base URL for approve/reject links.

    Returns:
        Tuple of (updated Invoice, ApprovalRecord).
    """
    logger.info(
        "Approval routing started",
        invoice_id=str(invoice.id),
        invoice_number=invoice.invoice_number,
        total=str(invoice.total),
    )

    amount = invoice.total or Decimal("0")
    tier = _get_approval_tier(amount, approval_matrix)
    token = _generate_approval_token()
    timeout_at = datetime.utcnow() + timedelta(hours=settings.approval_timeout_hours)

    approval = ApprovalRecord(
        id=uuid4(),
        invoice_id=invoice.id,
        approver_name=tier["approver_name"],
        approver_email=tier.get("approver_email", ""),
        approver_phone=tier.get("approver_phone"),
        approval_token=token,
        amount=amount,
        timeout_at=timeout_at,
    )

    # Auto-approve path
    if tier.get("auto_approve", False):
        approval.status = ApprovalStatus.APPROVED
        approval.decided_at = datetime.utcnow()
        invoice.status = InvoiceStatus.APPROVED
        invoice.approval_id = approval.id

        logger.info(
            "Invoice auto-approved",
            invoice_id=str(invoice.id),
            amount=str(amount),
            threshold=str(tier["max_amount"]),
        )
        return invoice, approval

    # Manual approval path
    approve_url = _build_approval_url(base_url, token, "approve")
    reject_url = _build_approval_url(base_url, token, "reject")

    email_sent = _send_approval_email(approval, invoice, approve_url, reject_url)
    sms_sent = _send_approval_sms(approval, invoice, approve_url)

    approval.status = ApprovalStatus.PENDING
    invoice.status = InvoiceStatus.PENDING_APPROVAL
    invoice.approval_id = approval.id

    logger.info(
        "Approval request sent",
        invoice_id=str(invoice.id),
        approver=tier["approver_name"],
        approver_email=tier.get("approver_email"),
        email_sent=email_sent,
        sms_sent=sms_sent,
        timeout_at=timeout_at.isoformat(),
    )

    return invoice, approval


def process_approval_decision(
    invoice: Invoice,
    approval: ApprovalRecord,
    decision: str,
    rejection_reason: Optional[str] = None,
) -> tuple[Invoice, ApprovalRecord]:
    """
    Process an approve or reject decision from the one-click link.
    Called by the approval webhook endpoint in the FastAPI router.

    Args:
        invoice: The associated Invoice to update.
        approval: The ApprovalRecord to update.
        decision: Either 'approve' or 'reject'.
        rejection_reason: Required if decision is 'reject'.

    Returns:
        Tuple of (updated Invoice, updated ApprovalRecord).
    """
    now = datetime.utcnow()

    if decision == "approve":
        approval.status = ApprovalStatus.APPROVED
        approval.decided_at = now
        invoice.status = InvoiceStatus.APPROVED

        logger.info(
            "Invoice approved",
            invoice_id=str(invoice.id),
            approver=approval.approver_name,
        )

    elif decision == "reject":
        approval.status = ApprovalStatus.REJECTED
        approval.decided_at = now
        approval.rejection_reason = rejection_reason
        invoice.status = InvoiceStatus.REJECTED

        logger.warning(
            "Invoice rejected",
            invoice_id=str(invoice.id),
            approver=approval.approver_name,
            reason=rejection_reason,
        )

    else:
        logger.error("Invalid approval decision", decision=decision)

    return invoice, approval