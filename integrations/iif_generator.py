# integrations/iif_generator.py
# QuickBooks Desktop IIF Batch File Generator
# Produces daily IIF import files per LLC entity from approved bills

import os
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IIF Format Constants
# ---------------------------------------------------------------------------

# QuickBooks IIF date format
IIF_DATE_FORMAT = "%m/%d/%Y"

# IIF record type headers
HDR_BILL = "!TRNS"
HDR_SPLIT = "!SPL"
HDR_END = "!ENDTRNS"

BILL_COLUMNS = [
    "TRNSTYPE", "DATE", "ACCNT", "NAME", "CLASS", "AMOUNT",
    "DOCNUM", "MEMO", "TOPRINT", "DUEDATE", "TERMS",
]

SPL_COLUMNS = [
    "TRNSTYPE", "DATE", "ACCNT", "NAME", "CLASS", "AMOUNT",
    "DOCNUM", "MEMO", "QNTY", "PRICE",
]

# ---------------------------------------------------------------------------
# Data classes (internal — not persisted, built from Airtable records)
# ---------------------------------------------------------------------------

@dataclass
class BillLineItem:
    """One line item on an approved AP bill."""
    gl_account: str          # GL account number for this entity
    amount: Decimal          # Positive amount (IIF negates for AP)
    cs_code: str             # CSI cost code, goes in MEMO field
    description: str         # Human-readable line description
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None


@dataclass
class ApprovedBill:
    """
    A fully approved AP bill ready for IIF export.
    Built from Airtable Bills table record after approval.
    """
    bill_id: str                          # Airtable record ID
    entity_name: str                      # LLC name (must match QB Desktop company file)
    vendor_name: str                      # Must match QB vendor list exactly
    invoice_number: str
    invoice_date: date
    due_date: Optional[date]
    ap_account: str                       # Accounts Payable GL account for this entity
    project_name: str                     # QB Desktop CLASS field
    terms: Optional[str] = None
    line_items: list[BillLineItem] = field(default_factory=list)


@dataclass
class IIFBatch:
    """Represents one IIF file for one entity on one date."""
    entity_name: str
    generation_date: date
    bills: list[ApprovedBill] = field(default_factory=list)


# ---------------------------------------------------------------------------
# IIF Generator
# ---------------------------------------------------------------------------

class IIFGenerator:
    """
    Generates QuickBooks Desktop IIF batch import files for AP bills.

    File layout per QB Desktop IIF specification:
    - Header row defining column names (!TRNS / !SPL)
    - One TRNS row per bill (the AP header transaction)
    - One SPL row per line item (the expense split)
    - ENDTRNS row to close each bill
    """

    def __init__(self, export_base_dir: str = "exports/iif"):
        self.export_base_dir = Path(export_base_dir)
        self.export_base_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Public: Generate IIF file for one entity
    # -----------------------------------------------------------------------

    def generate(self, batch: IIFBatch) -> str:
        """
        Generate an IIF file for all approved bills in the batch.

        Args:
            batch: IIFBatch with entity name and list of ApprovedBill records.

        Returns:
            Absolute path to the generated .iif file.
        """
        if not batch.bills:
            logger.info(f"No approved bills for {batch.entity_name} on {batch.generation_date}. Skipping.")
            return ""

        output_path = self._get_output_path(batch.entity_name, batch.generation_date)
        lines = self._build_iif_content(batch)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines), encoding="utf-8")

        logger.info(
            f"IIF file generated: {output_path} | "
            f"Entity: {batch.entity_name} | "
            f"Bills: {len(batch.bills)} | "
            f"Date: {batch.generation_date}"
        )
        return str(output_path)

    # -----------------------------------------------------------------------
    # Public: Generate IIF for multiple entities at once (daily batch run)
    # -----------------------------------------------------------------------

    def generate_daily_batch(self, batches: list[IIFBatch]) -> dict[str, str]:
        """
        Generate IIF files for all entities with pending approved bills.

        Args:
            batches: List of IIFBatch objects, one per entity.

        Returns:
            Dict mapping entity_name to generated file path.
            Entities with no bills return empty string.
        """
        results = {}
        for batch in batches:
            path = self.generate(batch)
            results[batch.entity_name] = path
        return results

    # -----------------------------------------------------------------------
    # Internal: Build IIF content lines
    # -----------------------------------------------------------------------

    def _build_iif_content(self, batch: IIFBatch) -> list[str]:
        """Build all lines of the IIF file."""
        lines = []

        # Column definition headers (written once at top of file)
        lines.append("\t".join([HDR_BILL] + BILL_COLUMNS))
        lines.append("\t".join([HDR_SPLIT] + SPL_COLUMNS))
        lines.append(HDR_END)
        lines.append("")  # blank separator

        for bill in batch.bills:
            lines.extend(self._build_bill_block(bill))
            lines.append("")  # blank line between bills

        return lines

    def _build_bill_block(self, bill: ApprovedBill) -> list[str]:
        """
        Build the TRNS + SPL + ENDTRNS block for one bill.

        QB Desktop IIF AP convention:
        - TRNS AMOUNT is negative (money owed to vendor, credit to AP)
        - SPL AMOUNT is positive (debit to expense GL account)
        """
        lines = []
        total_amount = sum(item.amount for item in bill.line_items)
        invoice_date_str = bill.invoice_date.strftime(IIF_DATE_FORMAT)
        due_date_str = bill.due_date.strftime(IIF_DATE_FORMAT) if bill.due_date else ""

        # TRNS row: the AP bill header
        trns_values = {
            "TRNSTYPE": "BILL",
            "DATE": invoice_date_str,
            "ACCNT": bill.ap_account,
            "NAME": bill.vendor_name,
            "CLASS": bill.project_name,
            "AMOUNT": self._fmt_amount(-total_amount),  # negative for AP credit
            "DOCNUM": bill.invoice_number,
            "MEMO": f"Bill {bill.invoice_number}",
            "TOPRINT": "N",
            "DUEDATE": due_date_str,
            "TERMS": bill.terms or "",
        }
        lines.append("TRNS\t" + "\t".join(trns_values[col] for col in BILL_COLUMNS))

        # SPL rows: one per line item
        for item in bill.line_items:
            spl_values = {
                "TRNSTYPE": "BILL",
                "DATE": invoice_date_str,
                "ACCNT": item.gl_account,
                "NAME": bill.vendor_name,
                "CLASS": bill.project_name,
                "AMOUNT": self._fmt_amount(item.amount),  # positive debit to expense
                "DOCNUM": bill.invoice_number,
                "MEMO": item.cs_code,  # CSI cost code in MEMO per spec
                "QNTY": self._fmt_qty(item.quantity),
                "PRICE": self._fmt_amount(item.unit_price) if item.unit_price else "",
            }
            lines.append("SPL\t" + "\t".join(spl_values[col] for col in SPL_COLUMNS))

        lines.append("ENDTRNS")
        return lines

    # -----------------------------------------------------------------------
    # Internal: Helpers
    # -----------------------------------------------------------------------

    def _get_output_path(self, entity_name: str, generation_date: date) -> Path:
        """
        Build output path: exports/iif/{entity_name}/YYYY-MM-DD.iif
        Entity name is sanitized for filesystem use.
        """
        safe_entity = self._sanitize_name(entity_name)
        date_str = generation_date.strftime("%Y-%m-%d")
        return self.export_base_dir / safe_entity / f"{date_str}.iif"

    def _sanitize_name(self, name: str) -> str:
        """Make entity name safe for use as a directory name."""
        return "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip()

    def _fmt_amount(self, amount) -> str:
        """Format Decimal or float as QB IIF amount string (2 decimal places)."""
        if amount is None:
            return "0.00"
        return f"{Decimal(str(amount)):.2f}"

    def _fmt_qty(self, qty) -> str:
        """Format quantity for IIF SPL row."""
        if qty is None:
            return ""
        return f"{Decimal(str(qty)):.4f}"

    # -----------------------------------------------------------------------
    # Public: Validate an existing IIF file (basic sanity check)
    # -----------------------------------------------------------------------

    def validate_iif_file(self, file_path: str) -> dict:
        """
        Basic structural validation of a generated IIF file.
        Checks that every TRNS has a matching ENDTRNS.

        Returns dict with: valid (bool), bill_count (int), errors (list).
        """
        path = Path(file_path)
        if not path.exists():
            return {"valid": False, "bill_count": 0, "errors": ["File not found."]}

        lines = path.read_text(encoding="utf-8").splitlines()
        errors = []
        trns_count = 0
        end_count = 0
        spl_count = 0

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("TRNS\t"):
                trns_count += 1
            elif stripped.startswith("SPL\t"):
                spl_count += 1
            elif stripped == "ENDTRNS":
                end_count += 1

        if trns_count != end_count:
            errors.append(
                f"TRNS/ENDTRNS mismatch: {trns_count} TRNS rows, {end_count} ENDTRNS rows."
            )

        if trns_count == 0:
            errors.append("No TRNS rows found. File may be empty or malformed.")

        return {
            "valid": len(errors) == 0,
            "bill_count": trns_count,
            "spl_line_count": spl_count,
            "errors": errors,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_generator: IIFGenerator | None = None


def get_iif_generator() -> IIFGenerator:
    """Return the shared IIFGenerator singleton."""
    global _generator
    if _generator is None:
        export_dir = os.getenv("IIF_EXPORT_DIR", "exports/iif")
        _generator = IIFGenerator(export_base_dir=export_dir)
    return _generator
