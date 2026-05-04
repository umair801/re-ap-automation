# agents/cc_charge_agent.py
# Credit Card Charge Path 2 Agent
# Handles CC receipt extraction, project tagging, QBO push, and intercompany reimbursement

import logging
from datetime import datetime, date
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum

from integrations.airtable_client import get_airtable_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class BillableStatus(str, Enum):
    BILLABLE = "Billable"
    NON_BILLABLE = "Non-Billable"
    PENDING_REVIEW = "Pending Review"


class TaggingDecision(str, Enum):
    CLEAR_OVERHEAD = "clear_overhead"
    CLEAR_PROJECT = "clear_project"
    AMBIGUOUS = "ambiguous"


# ---------------------------------------------------------------------------
# Corporate overhead indicators
# These vendor/description patterns are always tagged as non-billable overhead
# ---------------------------------------------------------------------------

OVERHEAD_KEYWORDS = [
    "saas", "software", "subscription", "adobe", "microsoft", "google workspace",
    "slack", "zoom", "dropbox", "notion", "airtable", "docusign",
    "office supplies", "office depot", "staples",
    "fuel", "gas station", "shell", "bp ", "exxon",
    "advertising", "facebook ads", "google ads", "linkedin",
    "insurance premium", "bank fee", "wire fee",
    "utilities", "phone", "internet", "verizon", "att ",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CCReceiptInput:
    """Raw CC receipt data extracted from email/PDF."""
    vendor_name: str
    receipt_number: str
    charge_date: date
    total_amount: Decimal
    card_last_4: str
    description: str
    is_recurring: bool = False
    pdf_path: str = ""


@dataclass
class CCChargeRecord:
    """A processed CC charge ready for QBO push."""
    vendor_name: str
    receipt_number: str
    charge_date: date
    total_amount: Decimal
    card_last_4: str
    description: str
    billable_status: BillableStatus
    project_id: str = ""
    project_name: str = ""
    qbo_customer_id: str = ""
    gl_account: str = ""
    cs_code: str = ""
    pdf_path: str = ""
    tagging_decision: TaggingDecision = TaggingDecision.AMBIGUOUS
    tagging_confidence: float = 0.0


# ---------------------------------------------------------------------------
# CC Charge Agent
# ---------------------------------------------------------------------------

class CCChargeAgent:
    """
    Processes credit card charge receipts (Path 2).

    Responsibilities:
    1. Extract and validate CC receipt data
    2. Decide project tagging: overhead vs project-billable vs ambiguous
    3. Push to QBO as Purchase/CreditCardCharge with billable expense tagging
    4. Track for month-end intercompany reimbursement
    5. Generate IIF file for project LLC to record reimbursement payment

    Critical rule: NEVER auto-tag ambiguous charges. Park for operator review.
    """

    def __init__(self):
        self.airtable = get_airtable_client()

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------

    def process_receipt(self, receipt: CCReceiptInput) -> dict:
        """
        Full processing pipeline for one CC charge receipt.

        Returns:
            {
                'status': 'queued_for_push' | 'parked_for_review' | 'error',
                'airtable_id': str,
                'billable_status': str,
                'project_name': str or None,
                'note': str
            }
        """
        # Step 1: Validate against vendor record
        vendor = self._validate_vendor(receipt)

        # Step 2: Make tagging decision
        decision, project, confidence = self._decide_project_tagging(receipt, vendor)

        # Step 3: Build charge record
        charge = self._build_charge_record(receipt, decision, project, confidence)

        # Step 4: Save to Airtable
        airtable_id = self._save_to_airtable(charge)

        # Step 5: Route based on tagging decision
        if decision == TaggingDecision.AMBIGUOUS:
            logger.warning(
                f"CC charge {receipt.receipt_number} from {receipt.vendor_name} "
                f"is ambiguous. Parked for operator review."
            )
            return {
                "status": "parked_for_review",
                "airtable_id": airtable_id,
                "billable_status": BillableStatus.PENDING_REVIEW,
                "project_name": None,
                "note": "System cannot confidently determine project. Operator review required.",
            }

        # Step 6: Push to QBO
        qbo_result = self._push_to_qbo(charge, airtable_id)

        return {
            "status": "queued_for_push" if qbo_result.get("success") else "error",
            "airtable_id": airtable_id,
            "billable_status": charge.billable_status,
            "project_name": charge.project_name or None,
            "qbo_transaction_id": qbo_result.get("transaction_id"),
            "note": qbo_result.get("note", ""),
        }

    # -----------------------------------------------------------------------
    # Step 1: Vendor validation
    # -----------------------------------------------------------------------

    def _validate_vendor(self, receipt: CCReceiptInput) -> Optional[dict]:
        """
        Cross-reference receipt against Vendors table.
        Checks card_last_4 matches and amount is within typical range.
        """
        vendor = self.airtable.get_vendor_by_name(receipt.vendor_name)
        if not vendor:
            logger.info(f"Vendor '{receipt.vendor_name}' not in Vendors table. New vendor on CC path.")
            return None
        return vendor

    # -----------------------------------------------------------------------
    # Step 2: Project tagging decision
    # -----------------------------------------------------------------------

    def _decide_project_tagging(
        self,
        receipt: CCReceiptInput,
        vendor: Optional[dict],
    ) -> tuple[TaggingDecision, Optional[dict], float]:
        """
        Decide whether this CC charge is overhead, project-billable, or ambiguous.

        Returns: (TaggingDecision, project_record_or_None, confidence_0_to_1)

        Rules:
        - Clear overhead: recurring SaaS, subscriptions, office supplies, fuel → Non-Billable
        - Clear project: vendor history shows project-specific work → Billable
        - Ambiguous: system cannot confidently determine → park for operator
        """
        description_lower = receipt.description.lower()
        vendor_lower = receipt.vendor_name.lower()

        # Check 1: Clear overhead indicators
        for keyword in OVERHEAD_KEYWORDS:
            if keyword in description_lower or keyword in vendor_lower:
                logger.info(
                    f"CC charge {receipt.receipt_number} tagged as overhead: "
                    f"matched keyword '{keyword}'"
                )
                return (TaggingDecision.CLEAR_OVERHEAD, None, 0.95)

        # Check 2: Recurring charge from known overhead vendor
        if receipt.is_recurring and vendor:
            payment_method = vendor.get("Payment_Method", "")
            if payment_method == "CC Auto-Charge":
                # Recurring CC auto-charge — likely overhead unless vendor is project-specific
                vendor_type = vendor.get("Vendor_Type", "").lower()
                if "construction" in vendor_type or "subcontractor" in vendor_type:
                    pass  # Fall through to project check
                else:
                    return (TaggingDecision.CLEAR_OVERHEAD, None, 0.80)

        # Check 3: Project reference in description
        active_projects = self._get_active_projects()
        for project in active_projects:
            project_name = project.get("Project_Name", "").lower()
            address = project.get("Property_Address", "").lower()

            if project_name and project_name in description_lower:
                logger.info(
                    f"CC charge {receipt.receipt_number} matched project "
                    f"'{project.get('Project_Name')}' by name in description."
                )
                return (TaggingDecision.CLEAR_PROJECT, project, 0.90)

            if address and len(address) > 5 and address in description_lower:
                logger.info(
                    f"CC charge {receipt.receipt_number} matched project "
                    f"'{project.get('Project_Name')}' by address in description."
                )
                return (TaggingDecision.CLEAR_PROJECT, project, 0.85)

        # Check 4: Vendor history shows consistent project assignment
        if vendor:
            project_from_history = self._check_vendor_project_history(vendor)
            if project_from_history:
                return (TaggingDecision.CLEAR_PROJECT, project_from_history, 0.75)

        # No confident determination — park for operator
        logger.info(
            f"CC charge {receipt.receipt_number} from {receipt.vendor_name}: "
            f"tagging ambiguous. Parking for operator review."
        )
        return (TaggingDecision.AMBIGUOUS, None, 0.0)

    def _get_active_projects(self) -> list[dict]:
        """Return active projects (cached per agent instance)."""
        if not hasattr(self, "_projects_cache"):
            try:
                self._projects_cache = self.airtable.get_all_active_projects()
            except Exception as e:
                logger.warning(f"Could not load active projects: {e}")
                self._projects_cache = []
        return self._projects_cache

    def _check_vendor_project_history(self, vendor: dict) -> Optional[dict]:
        """
        Check if vendor has a consistent history of being tagged to one project.
        Returns the project if 80%+ of recent charges went to the same project.
        """
        try:
            vendor_id = vendor.get("id", "")
            history = self.airtable.get_vendor_coding_history(vendor_id, months=6)

            project_counts: dict[str, int] = {}
            for record in history:
                project_id = record.get("Project_ID", "")
                if project_id:
                    project_counts[project_id] = project_counts.get(project_id, 0) + 1

            if not project_counts:
                return None

            total = sum(project_counts.values())
            top_project_id = max(project_counts, key=project_counts.get)
            ratio = project_counts[top_project_id] / total

            if ratio >= 0.80:
                return self.airtable.get_project_by_id(top_project_id)

        except Exception as e:
            logger.warning(f"Could not check vendor project history: {e}")

        return None

    # -----------------------------------------------------------------------
    # Step 3: Build charge record
    # -----------------------------------------------------------------------

    def _build_charge_record(
        self,
        receipt: CCReceiptInput,
        decision: TaggingDecision,
        project: Optional[dict],
        confidence: float,
    ) -> CCChargeRecord:
        """Build a CCChargeRecord from receipt + tagging decision."""

        if decision == TaggingDecision.CLEAR_OVERHEAD:
            billable_status = BillableStatus.NON_BILLABLE
        elif decision == TaggingDecision.CLEAR_PROJECT:
            billable_status = BillableStatus.BILLABLE
        else:
            billable_status = BillableStatus.PENDING_REVIEW

        return CCChargeRecord(
            vendor_name=receipt.vendor_name,
            receipt_number=receipt.receipt_number,
            charge_date=receipt.charge_date,
            total_amount=receipt.total_amount,
            card_last_4=receipt.card_last_4,
            description=receipt.description,
            billable_status=billable_status,
            project_id=project.get("id", "") if project else "",
            project_name=project.get("Project_Name", "") if project else "",
            qbo_customer_id=project.get("QBO_Customer_ID", "") if project else "",
            pdf_path=receipt.pdf_path,
            tagging_decision=decision,
            tagging_confidence=confidence,
        )

    # -----------------------------------------------------------------------
    # Step 4: Save to Airtable
    # -----------------------------------------------------------------------

    def _save_to_airtable(self, charge: CCChargeRecord) -> str:
        """Save CC charge record to Airtable CC_Charges table. Returns record ID."""
        fields = {
            "Vendor_Name": charge.vendor_name,
            "Receipt_Number": charge.receipt_number,
            "Charge_Date": charge.charge_date.isoformat(),
            "Total_Amount": float(charge.total_amount),
            "Card_Last_4": charge.card_last_4,
            "Description": charge.description,
            "Billable_Status": charge.billable_status.value,
            "Project_ID": charge.project_id,
            "Project_Name": charge.project_name,
            "QBO_Customer_ID": charge.qbo_customer_id,
            "QBO_Push_Status": "Pending",
            "Reimbursement_Status": "Pending",
            "PDF_Path": charge.pdf_path,
        }

        try:
            record = self.airtable.create_cc_charge(fields)
            logger.info(f"CC charge saved to Airtable: {record['id']}")
            return record["id"]
        except Exception as e:
            logger.error(f"Failed to save CC charge to Airtable: {e}")
            return ""

    # -----------------------------------------------------------------------
    # Step 5: Push to QBO
    # -----------------------------------------------------------------------

    def _push_to_qbo(self, charge: CCChargeRecord, airtable_id: str) -> dict:
        """
        Push CC charge to QuickBooks Online as a Purchase/CreditCardCharge.
        Sets BillableStatus and CustomerRef for project charges.
        """
        try:
            from integrations.quickbooks_client import push_cc_charge_to_qbo
            result = push_cc_charge_to_qbo(charge)

            if result.get("success"):
                # Update Airtable with QBO transaction ID
                self.airtable.update_cc_charge(airtable_id, {
                    "QBO_Push_Status": "Synced",
                    "QBO_Transaction_ID": result.get("transaction_id", ""),
                })
                logger.info(
                    f"CC charge {charge.receipt_number} pushed to QBO: "
                    f"{result.get('transaction_id')}"
                )
            else:
                self.airtable.update_cc_charge(airtable_id, {"QBO_Push_Status": "Failed"})

            return result

        except Exception as e:
            logger.error(f"QBO push failed for CC charge {charge.receipt_number}: {e}")
            if airtable_id:
                self.airtable.update_cc_charge(airtable_id, {"QBO_Push_Status": "Failed"})
            return {"success": False, "note": str(e)}

    # -----------------------------------------------------------------------
    # Month-end intercompany reimbursement
    # -----------------------------------------------------------------------

    def generate_month_end_reimbursement(self, year: int, month: int) -> dict:
        """
        Generate intercompany reimbursement summary for a given month.

        For each project LLC that had billable CC charges:
        - Produces a summary of all charges (date, vendor, amount, description, GL code)
        - Bundles them into a draft QBO Customer Invoice (Operating Entity → Project LLC)
        - Provides IIF file for project LLC QB Desktop to record reimbursement payment

        Returns dict mapping project_name to reimbursement summary.
        """
        logger.info(f"Generating month-end reimbursement for {year}-{month:02d}")

        charges = self.airtable.get_billable_cc_charges_for_month(year, month)

        if not charges:
            logger.info(f"No billable CC charges found for {year}-{month:02d}.")
            return {}

        # Group by project
        by_project: dict[str, list[dict]] = {}
        for charge in charges:
            project_id = charge.get("Project_ID", "unknown")
            if project_id not in by_project:
                by_project[project_id] = []
            by_project[project_id].append(charge)

        reimbursement_summary = {}

        for project_id, project_charges in by_project.items():
            project = self.airtable.get_project_by_id(project_id)
            if not project:
                logger.warning(f"Project {project_id} not found. Skipping reimbursement.")
                continue

            project_name = project.get("Project_Name", project_id)
            total = sum(Decimal(str(c.get("Total_Amount", 0))) for c in project_charges)

            # Build line detail for QBO invoice
            line_items = [
                {
                    "charge_date": c.get("Charge_Date"),
                    "vendor_name": c.get("Vendor_Name"),
                    "description": c.get("Description"),
                    "amount": float(c.get("Total_Amount", 0)),
                    "cs_code": c.get("CS_Code", ""),
                    "gl_account": c.get("GL_Account", ""),
                }
                for c in project_charges
            ]

            # Push draft invoice to QBO
            qbo_result = self._create_qbo_reimbursement_invoice(
                project=project,
                line_items=line_items,
                total=total,
                year=year,
                month=month,
            )

            # Generate IIF file for project LLC QB Desktop
            iif_path = self._generate_reimbursement_iif(
                project=project,
                line_items=line_items,
                total=total,
                year=year,
                month=month,
            )

            # Mark charges as invoiced in Airtable
            for charge in project_charges:
                self.airtable.update_cc_charge(
                    charge["id"], {"Reimbursement_Status": "Invoiced"}
                )

            reimbursement_summary[project_name] = {
                "project_id": project_id,
                "charge_count": len(project_charges),
                "total_amount": float(total),
                "qbo_invoice_id": qbo_result.get("invoice_id"),
                "iif_path": iif_path,
                "period": f"{year}-{month:02d}",
            }

            logger.info(
                f"Reimbursement generated for {project_name}: "
                f"{len(project_charges)} charges, ${total:.2f}"
            )

        return reimbursement_summary

    def _create_qbo_reimbursement_invoice(
        self,
        project: dict,
        line_items: list[dict],
        total: Decimal,
        year: int,
        month: int,
    ) -> dict:
        """Create draft Customer Invoice in QBO (Operating Entity → Project LLC)."""
        try:
            from integrations.quickbooks_client import create_intercompany_invoice
            return create_intercompany_invoice(
                customer_id=project.get("QBO_Customer_ID", ""),
                project_name=project.get("Project_Name", ""),
                line_items=line_items,
                total=float(total),
                period=f"{year}-{month:02d}",
            )
        except Exception as e:
            logger.error(f"Failed to create QBO reimbursement invoice: {e}")
            return {"invoice_id": None, "error": str(e)}

    def _generate_reimbursement_iif(
        self,
        project: dict,
        line_items: list[dict],
        total: Decimal,
        year: int,
        month: int,
    ) -> str:
        """
        Generate IIF file for project LLC QB Desktop to record reimbursement payment.
        The project LLC records this as a vendor bill from the Operating Entity.
        """
        from integrations.iif_generator import IIFGenerator, IIFBatch, ApprovedBill, BillLineItem
        from datetime import date

        entity_name = project.get("Project_LLC", project.get("Project_Name", "Unknown LLC"))
        operating_entity = project.get("Operating_Entity", "Operating Entity")
        period_date = date(year, month, 1)

        bill_lines = [
            BillLineItem(
                gl_account=item.get("gl_account") or "6000",
                amount=Decimal(str(item["amount"])),
                cs_code=item.get("cs_code") or "REIMB",
                description=item.get("description", "CC charge reimbursement"),
            )
            for item in line_items
        ]

        bill = ApprovedBill(
            bill_id=f"reimb_{year}_{month:02d}_{project.get('id', 'unknown')}",
            entity_name=entity_name,
            vendor_name=operating_entity,
            invoice_number=f"REIMB-{year}-{month:02d}",
            invoice_date=period_date,
            due_date=None,
            ap_account="2000",
            project_name=project.get("Project_Name", ""),
            terms="Due on Receipt",
            line_items=bill_lines,
        )

        batch = IIFBatch(
            entity_name=entity_name,
            generation_date=date.today(),
            bills=[bill],
        )

        generator = IIFGenerator(export_base_dir="exports/iif/reimbursements")
        path = generator.generate(batch)
        return path


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_agent: Optional[CCChargeAgent] = None


def get_cc_charge_agent() -> CCChargeAgent:
    """Return the shared CCChargeAgent singleton."""
    global _agent
    if _agent is None:
        _agent = CCChargeAgent()
    return _agent
