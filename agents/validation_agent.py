# agents/validation_agent.py

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from core.config import get_settings
from core.logger import get_logger
from core.models import (
    Invoice,
    InvoiceStatus,
    ValidationError,
)

logger = get_logger(__name__)
settings = get_settings()


# ─── Business Rule Validators ─────────────────────────────────────────────────

def _check_required_fields(invoice: Invoice) -> list[ValidationError]:
    """Verify all mandatory fields are present on the invoice."""
    errors: list[ValidationError] = []

    required = {
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date,
        "due_date": invoice.due_date,
        "vendor": invoice.vendor,
        "total": invoice.total,
    }

    for field_name, value in required.items():
        if value is None:
            errors.append(
                ValidationError(
                    field=field_name,
                    error_code="MISSING_REQUIRED_FIELD",
                    message=f"Required field '{field_name}' is missing from the invoice.",
                )
            )

    # Vendor name specifically
    if invoice.vendor and not invoice.vendor.vendor_name.strip():
        errors.append(
            ValidationError(
                field="vendor_name",
                error_code="EMPTY_VENDOR_NAME",
                message="Vendor name is present but empty.",
            )
        )

    return errors


def _check_amount_consistency(invoice: Invoice) -> list[ValidationError]:
    """
    Verify that subtotal + tax equals total within a small rounding tolerance.
    Also checks that all amounts are positive.
    """
    errors: list[ValidationError] = []
    tolerance = Decimal("0.02")  # Allow 2 cent rounding difference

    if invoice.total is not None and invoice.total <= Decimal("0"):
        errors.append(
            ValidationError(
                field="total",
                error_code="INVALID_AMOUNT",
                message=f"Invoice total must be greater than zero. Got: {invoice.total}",
            )
        )

    if invoice.subtotal is not None and invoice.subtotal < Decimal("0"):
        errors.append(
            ValidationError(
                field="subtotal",
                error_code="NEGATIVE_SUBTOTAL",
                message=f"Subtotal cannot be negative. Got: {invoice.subtotal}",
            )
        )

    if invoice.tax is not None and invoice.tax < Decimal("0"):
        errors.append(
            ValidationError(
                field="tax",
                error_code="NEGATIVE_TAX",
                message=f"Tax amount cannot be negative. Got: {invoice.tax}",
            )
        )

    # Check subtotal + tax = total (only if all three are present)
    if (
        invoice.subtotal is not None
        and invoice.tax is not None
        and invoice.total is not None
    ):
        calculated = invoice.subtotal + invoice.tax
        variance = abs(calculated - invoice.total)

        if variance > tolerance:
            errors.append(
                ValidationError(
                    field="total",
                    error_code="AMOUNT_MISMATCH",
                    message=(
                        f"Subtotal ({invoice.subtotal}) + Tax ({invoice.tax}) = "
                        f"{calculated}, but invoice total is {invoice.total}. "
                        f"Variance: {variance}"
                    ),
                )
            )

    return errors


def _check_dates(invoice: Invoice) -> list[ValidationError]:
    """
    Verify invoice dates are valid:
    - Invoice date is not in the future
    - Due date is not before invoice date
    - Due date is not excessively overdue (more than 365 days past)
    """
    errors: list[ValidationError] = []
    today = date.today()

    if invoice.invoice_date is not None:
        if invoice.invoice_date > today:
            errors.append(
                ValidationError(
                    field="invoice_date",
                    error_code="FUTURE_INVOICE_DATE",
                    message=f"Invoice date {invoice.invoice_date} is in the future.",
                )
            )

    if invoice.due_date is not None and invoice.invoice_date is not None:
        if invoice.due_date < invoice.invoice_date:
            errors.append(
                ValidationError(
                    field="due_date",
                    error_code="DUE_DATE_BEFORE_INVOICE_DATE",
                    message=(
                        f"Due date {invoice.due_date} is before "
                        f"invoice date {invoice.invoice_date}."
                    ),
                )
            )

    if invoice.due_date is not None:
        days_overdue = (today - invoice.due_date).days
        if days_overdue > 365:
            errors.append(
                ValidationError(
                    field="due_date",
                    error_code="EXCESSIVELY_OVERDUE",
                    message=(
                        f"Invoice due date {invoice.due_date} is "
                        f"{days_overdue} days past. Possible extraction error."
                    ),
                )
            )

    return errors


def _check_currency(invoice: Invoice) -> list[ValidationError]:
    """Verify currency is a valid 3-letter ISO code."""
    errors: list[ValidationError] = []

    valid_currencies = {
        "USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CHF",
        "CNY", "INR", "SGD", "HKD", "NOK", "SEK", "DKK",
        "NZD", "MXN", "BRL", "ZAR", "AED", "SAR",
    }

    if not invoice.currency or len(invoice.currency) != 3:
        errors.append(
            ValidationError(
                field="currency",
                error_code="INVALID_CURRENCY_FORMAT",
                message=f"Currency must be a 3-letter ISO code. Got: '{invoice.currency}'",
            )
        )
    elif invoice.currency.upper() not in valid_currencies:
        errors.append(
            ValidationError(
                field="currency",
                error_code="UNRECOGNIZED_CURRENCY",
                message=(
                    f"Currency '{invoice.currency}' is not in the recognized list. "
                    "Verify before processing."
                ),
            )
        )

    return errors


def _check_line_items(invoice: Invoice) -> list[ValidationError]:
    """
    Verify line item totals are consistent with the invoice total.
    Warns if line items are missing entirely.
    """
    errors: list[ValidationError] = []

    if not invoice.line_items:
        # Not a hard error — some invoices have no itemized lines
        logger.info(
            "Invoice has no line items",
            invoice_id=str(invoice.id),
            invoice_number=invoice.invoice_number,
        )
        return errors

    # Check each line item has positive quantities and prices
    for item in invoice.line_items:
        if item.quantity <= Decimal("0"):
            errors.append(
                ValidationError(
                    field=f"line_item_{item.line_number}.quantity",
                    error_code="INVALID_LINE_QUANTITY",
                    message=f"Line {item.line_number} has invalid quantity: {item.quantity}",
                )
            )

        if item.unit_price < Decimal("0"):
            errors.append(
                ValidationError(
                    field=f"line_item_{item.line_number}.unit_price",
                    error_code="NEGATIVE_UNIT_PRICE",
                    message=(
                        f"Line {item.line_number} has negative unit price: {item.unit_price}"
                    ),
                )
            )

    # Check sum of line item totals matches invoice total
    if invoice.total is not None and len(invoice.line_items) > 0:
        line_sum = sum(item.total for item in invoice.line_items)
        variance = abs(line_sum - invoice.total)
        tolerance = invoice.total * Decimal("0.02")  # 2% tolerance

        if variance > tolerance:
            errors.append(
                ValidationError(
                    field="line_items_total",
                    error_code="LINE_ITEMS_TOTAL_MISMATCH",
                    message=(
                        f"Sum of line items ({line_sum}) does not match "
                        f"invoice total ({invoice.total}). Variance: {variance}"
                    ),
                )
            )

    return errors


def _check_extraction_confidence(invoice: Invoice) -> list[ValidationError]:
    """Flag invoices where GPT-4o extraction confidence was low."""
    errors: list[ValidationError] = []

    if (
        invoice.extraction_confidence is not None
        and invoice.extraction_confidence < 0.5
    ):
        errors.append(
            ValidationError(
                field="extraction_confidence",
                error_code="LOW_EXTRACTION_CONFIDENCE",
                message=(
                    f"Extraction confidence is {invoice.extraction_confidence:.2f}. "
                    "Manual review recommended."
                ),
            )
        )

    return errors


def _check_duplicate(
    invoice: Invoice,
    existing_invoice_numbers: Optional[list[str]] = None,
) -> list[ValidationError]:
    """
    Check if this invoice number has already been processed.
    In production this queries Supabase. Here we accept an optional list
    for testing purposes.
    """
    errors: list[ValidationError] = []

    if invoice.invoice_number is None:
        return errors

    known = existing_invoice_numbers or []

    if invoice.invoice_number in known:
        errors.append(
            ValidationError(
                field="invoice_number",
                error_code="DUPLICATE_INVOICE",
                message=(
                    f"Invoice number '{invoice.invoice_number}' has already "
                    "been processed. Possible duplicate submission."
                ),
            )
        )

    return errors


# ─── Main Agent ───────────────────────────────────────────────────────────────

def run_validation_agent(
    invoice: Invoice,
    existing_invoice_numbers: Optional[list[str]] = None,
) -> Invoice:
    """
    Main entry point for the Validation Agent.

    Runs all business rule checks against the extracted invoice.
    Populates invoice.validation_errors with any failures found.
    Sets status to VALIDATED (clean) or EXCEPTION (has errors).

    Args:
        invoice: The Invoice model populated by the Extraction Agent.
        existing_invoice_numbers: List of already-processed invoice numbers
            for duplicate detection. In production, fetched from Supabase.

    Returns:
        Invoice with updated status and validation_errors list.
    """
    logger.info(
        "Validation started",
        invoice_id=str(invoice.id),
        invoice_number=invoice.invoice_number,
    )

    invoice.status = InvoiceStatus.VALIDATING
    all_errors: list[ValidationError] = []

    # Run all checks
    all_errors.extend(_check_required_fields(invoice))
    all_errors.extend(_check_amount_consistency(invoice))
    all_errors.extend(_check_dates(invoice))
    all_errors.extend(_check_currency(invoice))
    all_errors.extend(_check_line_items(invoice))
    all_errors.extend(_check_extraction_confidence(invoice))
    all_errors.extend(_check_duplicate(invoice, existing_invoice_numbers))

    invoice.validation_errors = all_errors

    if all_errors:
        invoice.status = InvoiceStatus.EXCEPTION
        logger.warning(
            "Validation failed",
            invoice_id=str(invoice.id),
            invoice_number=invoice.invoice_number,
            error_count=len(all_errors),
            errors=[e.error_code for e in all_errors],
        )
    else:
        invoice.status = InvoiceStatus.VALIDATED
        logger.info(
            "Validation passed",
            invoice_id=str(invoice.id),
            invoice_number=invoice.invoice_number,
        )

    return invoice