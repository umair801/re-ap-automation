# agents/audit_logger_agent.py

import uuid
from datetime import datetime, timezone
from typing import Optional
import structlog
from core.models import (
    AuditEntry,
    Invoice,
    InvoiceStatus,
    ERPSyncResult,
    ApprovalRecord,
    ExceptionRecord,
    PaymentRecord,
)
from notifications.email_sender import send_email
from notifications.sms_sender import send_sms
from core.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()


class AuditLoggerAgent:
    """Writes audit trail entries and sends AP manager notifications."""

    def __init__(self) -> None:
        self.log = logger.bind(agent="audit_logger_agent")
        self.audit_trail: list[dict] = []

    # ------------------------------------------------------------------
    # Audit trail writers
    # ------------------------------------------------------------------

    def log_invoice_received(self, invoice: Invoice) -> dict:
        return self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="invoice_received",
            from_status=None,
            to_status=InvoiceStatus.RECEIVED,
            details=f"Invoice received. Vendor: {invoice.vendor.vendor_name if invoice.vendor else 'unknown'}",
        )

    def log_extraction_complete(self, invoice: Invoice) -> dict:
        return self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="extraction_complete",
            from_status=InvoiceStatus.RECEIVED,
            to_status=InvoiceStatus.EXTRACTED,
            details=f"Fields extracted. Total: {invoice.total} {invoice.currency}",
        )

    def log_validation_passed(self, invoice: Invoice) -> dict:
        return self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="validation_passed",
            from_status=InvoiceStatus.EXTRACTED,
            to_status=InvoiceStatus.VALIDATED,
            details="All validation checks passed.",
        )

    def log_validation_failed(self, invoice: Invoice, reason: str) -> dict:
        entry = self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="validation_failed",
            from_status=InvoiceStatus.EXTRACTED,
            to_status=InvoiceStatus.EXCEPTION,
            details=f"Validation failed: {reason}",
        )
        self._notify_exception(invoice.invoice_number or "unknown", reason)
        return entry

    def log_match_passed(self, invoice: Invoice) -> dict:
        return self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="three_way_match_passed",
            from_status=InvoiceStatus.VALIDATED,
            to_status=InvoiceStatus.MATCHED,
            details="Three-way match passed.",
        )

    def log_match_failed(self, invoice: Invoice, reason: str) -> dict:
        entry = self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="three_way_match_failed",
            from_status=InvoiceStatus.VALIDATED,
            to_status=InvoiceStatus.EXCEPTION,
            details=f"Three-way match failed: {reason}",
        )
        self._notify_exception(invoice.invoice_number or "unknown", reason)
        return entry

    def log_approval_requested(self, invoice: Invoice, approval: ApprovalRecord) -> dict:
        return self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="approval_requested",
            from_status=InvoiceStatus.MATCHED,
            to_status=InvoiceStatus.PENDING_APPROVAL,
            details=f"Approval requested from: {approval.approver_email}",
        )

    def log_approved(self, invoice: Invoice, approval: ApprovalRecord) -> dict:
        entry = self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="invoice_approved",
            from_status=InvoiceStatus.PENDING_APPROVAL,
            to_status=InvoiceStatus.APPROVED,
            details=f"Approved by: {approval.approver_email}",
        )
        self._notify_approval(invoice.invoice_number or "unknown", approval.approver_email)
        return entry

    def log_rejected(self, invoice: Invoice, approval: ApprovalRecord, reason: str) -> dict:
        entry = self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="invoice_rejected",
            from_status=InvoiceStatus.PENDING_APPROVAL,
            to_status=InvoiceStatus.EXCEPTION,
            details=f"Rejected by: {approval.approver_email}. Reason: {reason}",
        )
        self._notify_exception(invoice.invoice_number or "unknown", f"Rejected: {reason}")
        return entry

    def log_payment_scheduled(self, invoice: Invoice, payment: PaymentRecord) -> dict:
        return self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="payment_scheduled",
            from_status=InvoiceStatus.APPROVED,
            to_status=InvoiceStatus.SCHEDULED,
            details=f"Payment of {payment.amount} scheduled for {payment.scheduled_date}",
        )

    def log_payment_complete(self, invoice: Invoice, payment: PaymentRecord) -> dict:
        entry = self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="payment_complete",
            from_status=InvoiceStatus.SCHEDULED,
            to_status=InvoiceStatus.PAID,
            details=f"Payment confirmed. Amount: {payment.amount}",
        )
        self._notify_payment(invoice.invoice_number or "unknown", float(payment.amount))
        return entry

    def log_erp_sync(self, invoice: Invoice, sync_result: ERPSyncResult) -> dict:
        status = "erp_sync_success" if sync_result.success else "erp_sync_failed"
        details = (
            f"ERP: {sync_result.erp_provider.value}. Transaction ID: {sync_result.erp_transaction_id}"
            if sync_result.success
            else f"ERP sync failed: {sync_result.error_message}"
        )
        return self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event=status,
            from_status=None,
            to_status=None,
            details=details,
        )

    def log_exception(self, invoice: Invoice, exception: ExceptionRecord) -> dict:
        return self._write_entry(
            invoice_number=invoice.invoice_number or "unknown",
            event="exception_created",
            from_status=None,
            to_status=InvoiceStatus.EXCEPTION,
            details=f"Exception: {exception.exception_type} - {exception.description}",
        )

    # ------------------------------------------------------------------
    # Daily summary report
    # ------------------------------------------------------------------

    def generate_daily_summary(self) -> str:
        total = len(self.audit_trail)
        events: dict[str, int] = {}
        for entry in self.audit_trail:
            events[entry["event"]] = events.get(entry["event"], 0) + 1

        lines = [
            f"AP-AI Daily Summary - {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
            f"Total audit events recorded: {total}",
            "",
            "Event breakdown:",
        ]
        for event, count in sorted(events.items()):
            lines.append(f"  {event}: {count}")

        report = "\n".join(lines)
        self.log.info("daily_summary_generated", total_events=total)
        return report

    def send_daily_summary(self) -> None:
        report = self.generate_daily_summary()
        send_email(
            to_email=settings.AP_MANAGER_EMAIL,
            subject="AP-AI Daily Summary Report",
            body=report,
        )
        self.log.info("daily_summary_sent", recipient=settings.AP_MANAGER_EMAIL)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_entry(
        self,
        invoice_number: str,
        event: str,
        from_status: Optional[InvoiceStatus],
        to_status: Optional[InvoiceStatus],
        details: str,
    ) -> dict:
        entry = {
            "entry_id": str(uuid.uuid4()),
            "invoice_number": invoice_number,
            "event": event,
            "from_status": from_status.value if from_status else None,
            "to_status": to_status.value if to_status else None,
            "details": details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.audit_trail.append(entry)
        self.log.info(
            "audit_entry_written",
            invoice_number=invoice_number,
            audit_event=event,
            to_status=str(to_status),
        )
        return entry

    def _notify_exception(self, invoice_number: str, reason: str) -> None:
        subject = f"AP-AI Exception: Invoice {invoice_number}"
        body = f"Invoice {invoice_number} has been flagged.\n\nReason: {reason}"
        try:
            send_email(to_email=settings.AP_MANAGER_EMAIL, subject=subject, body=body)
        except Exception as exc:
            self.log.warning("exception_email_failed", error=str(exc))
        try:
            send_sms(
                to_number=settings.AP_MANAGER_PHONE,
                message=f"AP-AI: Invoice {invoice_number} flagged. {reason[:100]}",
            )
        except Exception as exc:
            self.log.warning("exception_sms_failed", error=str(exc))

    def _notify_approval(self, invoice_number: str, approver: str) -> None:
        try:
            send_email(
                to_email=settings.AP_MANAGER_EMAIL,
                subject=f"AP-AI Approved: Invoice {invoice_number}",
                body=f"Invoice {invoice_number} was approved by {approver}.",
            )
        except Exception as exc:
            self.log.warning("approval_email_failed", error=str(exc))

    def _notify_payment(self, invoice_number: str, amount: float) -> None:
        try:
            send_email(
                to_email=settings.AP_MANAGER_EMAIL,
                subject=f"AP-AI Payment Confirmed: Invoice {invoice_number}",
                body=f"Payment of ${amount:,.2f} confirmed for invoice {invoice_number}.",
            )
        except Exception as exc:
            self.log.warning("payment_email_failed", error=str(exc))
