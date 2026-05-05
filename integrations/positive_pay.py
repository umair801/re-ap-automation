# integrations/positive_pay.py
# Positive Pay file generator for AP Automation System
# Generates daily check register files per entity for bank fraud protection
# Banks only honor checks that appear in this file

import os
import csv
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Most US banks accept one of these formats:
# 1. Fixed-width (most common for legacy bank systems)
# 2. CSV (increasingly common)
# 3. BAI2 (enterprise)
#
# We generate CSV as default (operator can request fixed-width per bank spec)
# ---------------------------------------------------------------------------

EXPORT_DIR = os.getenv("POSITIVE_PAY_EXPORT_DIR", "exports/positive_pay")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CheckRecord:
    """One check issued from an entity's bank account."""
    check_number: str
    amount: Decimal
    payee_name: str          # Must match vendor name on check exactly
    issue_date: date
    account_number: str      # Bank account number for this entity
    routing_number: str      # Bank routing number for this entity
    void: bool = False       # True if this check was voided
    memo: str = ""


@dataclass
class PositivePayBatch:
    """Daily Positive Pay batch for one entity."""
    entity_name: str
    bank_name: str
    account_number: str
    routing_number: str
    generation_date: date
    checks: list[CheckRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Positive Pay Generator
# ---------------------------------------------------------------------------

class PositivePayGenerator:
    """
    Generates Positive Pay check register files for bank fraud protection.

    The bank receives this file daily and only honors checks that appear here.
    Any check presented to the bank that is NOT in this file is flagged as
    potentially fraudulent.

    Supports two output formats:
    - CSV: standard comma-separated (default)
    - Fixed-width: for banks requiring legacy format
    """

    def __init__(self, export_dir: str = EXPORT_DIR):
        self.export_dir = Path(export_dir)
        self.export_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Public: Generate Positive Pay file
    # -----------------------------------------------------------------------

    def generate(
        self,
        batch: PositivePayBatch,
        format: str = "csv",
    ) -> str:
        """
        Generate a Positive Pay file for one entity.

        Args:
            batch:  PositivePayBatch with entity info and check records.
            format: 'csv' or 'fixed' (fixed-width).

        Returns:
            Absolute path to generated file, or empty string if no checks.
        """
        if not batch.checks:
            logger.info(
                f"Positive Pay: No checks for {batch.entity_name} "
                f"on {batch.generation_date}. Skipping."
            )
            return ""

        output_path = self._get_output_path(
            batch.entity_name, batch.generation_date, format
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if format == "fixed":
            self._write_fixed_width(batch, output_path)
        else:
            self._write_csv(batch, output_path)

        logger.info(
            f"Positive Pay file generated: {output_path} | "
            f"Entity: {batch.entity_name} | "
            f"Checks: {len(batch.checks)} | "
            f"Total: ${sum(c.amount for c in batch.checks):,.2f}"
        )
        return str(output_path)

    def generate_daily_batch(
        self, batches: list[PositivePayBatch], format: str = "csv"
    ) -> dict[str, str]:
        """Generate Positive Pay files for all entities."""
        return {
            batch.entity_name: self.generate(batch, format)
            for batch in batches
        }

    # -----------------------------------------------------------------------
    # CSV format
    # -----------------------------------------------------------------------

    def _write_csv(self, batch: PositivePayBatch, output_path: Path):
        """
        Write CSV Positive Pay file.

        Standard columns most banks accept:
        AccountNumber, CheckNumber, Amount, PayeeName, IssueDate, VoidFlag
        """
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            # Header row
            writer.writerow([
                "AccountNumber",
                "CheckNumber",
                "IssueDate",
                "Amount",
                "PayeeName",
                "VoidFlag",
                "Memo",
            ])

            # One row per check
            for check in batch.checks:
                writer.writerow([
                    batch.account_number,
                    check.check_number,
                    check.issue_date.strftime("%m/%d/%Y"),
                    f"{check.amount:.2f}",
                    check.payee_name,
                    "V" if check.void else "I",  # I=Issue, V=Void
                    check.memo,
                ])

    # -----------------------------------------------------------------------
    # Fixed-width format
    # -----------------------------------------------------------------------

    def _write_fixed_width(self, batch: PositivePayBatch, output_path: Path):
        """
        Write fixed-width Positive Pay file.

        Common layout (adjust per bank spec):
        Pos 01-10:  Account Number (right-justified, zero-padded)
        Pos 11-20:  Check Number (right-justified, zero-padded)
        Pos 21-28:  Issue Date (MMDDYYYY)
        Pos 29-40:  Amount (12 digits, no decimal, implied 2 decimal places)
        Pos 41-75:  Payee Name (35 chars, left-justified, space-padded)
        Pos 76:     Void Flag (I=Issue, V=Void)
        """
        lines = []
        for check in batch.checks:
            account = batch.account_number.rjust(10, "0")[:10]
            check_num = check.check_number.rjust(10, "0")[:10]
            issue_date = check.issue_date.strftime("%m%d%Y")
            amount_cents = int(check.amount * 100)
            amount_str = str(amount_cents).rjust(12, "0")[:12]
            payee = check.payee_name.ljust(35)[:35]
            void_flag = "V" if check.void else "I"

            line = f"{account}{check_num}{issue_date}{amount_str}{payee}{void_flag}"
            lines.append(line)

        output_path.write_text("\n".join(lines), encoding="utf-8")

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def validate_file(self, file_path: str) -> dict:
        """
        Validate a generated Positive Pay CSV file.
        Checks row count, amount format, required fields.
        """
        path = Path(file_path)
        if not path.exists():
            return {"valid": False, "errors": ["File not found."]}

        errors = []
        check_count = 0
        total_amount = Decimal("0")

        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, 1):
                check_count += 1

                # Check required fields
                for field_name in ["AccountNumber", "CheckNumber", "IssueDate", "Amount", "PayeeName"]:
                    if not row.get(field_name, "").strip():
                        errors.append(f"Row {i}: Missing {field_name}")

                # Validate amount
                try:
                    total_amount += Decimal(row.get("Amount", "0"))
                except Exception:
                    errors.append(f"Row {i}: Invalid amount '{row.get('Amount')}'")

        return {
            "valid": len(errors) == 0,
            "check_count": check_count,
            "total_amount": float(total_amount),
            "errors": errors,
        }

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _get_output_path(
        self, entity_name: str, generation_date: date, format: str
    ) -> Path:
        """Build output path: exports/positive_pay/{entity_name}/YYYY-MM-DD.{ext}"""
        safe_entity = "".join(
            c if c.isalnum() or c in "-_ " else "_" for c in entity_name
        ).strip()
        ext = "csv" if format == "csv" else "txt"
        date_str = generation_date.strftime("%Y-%m-%d")
        return self.export_dir / safe_entity / f"{date_str}.{ext}"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_generator: Optional[PositivePayGenerator] = None


def get_positive_pay_generator() -> PositivePayGenerator:
    """Return the shared PositivePayGenerator singleton."""
    global _generator
    if _generator is None:
        _generator = PositivePayGenerator()
    return _generator
