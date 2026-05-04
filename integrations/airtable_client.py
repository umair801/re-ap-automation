# integrations/airtable_client.py
# Airtable integration for AP Automation System
# Wraps pyairtable for all table operations across the seven core tables

import os
import logging
from typing import Optional
from datetime import date, datetime
from pyairtable import Api

load_dotenv_needed = True
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Table name constants (match Airtable base exactly)
# ---------------------------------------------------------------------------

TABLE_VENDORS = "Vendors"
TABLE_PROJECTS = "Projects"
TABLE_BILLS = "Bills"
TABLE_CC_CHARGES = "CC_Charges"
TABLE_AI_PARTIES = "AdditionalInsuredParties"
TABLE_CS_CODES = "CS_Codes"
TABLE_CS_TO_GL = "CS_to_GL_Mapping"


# ---------------------------------------------------------------------------
# Airtable Client
# ---------------------------------------------------------------------------

class AirtableClient:
    """
    Thin wrapper around pyairtable for all AP Automation System table access.
    One instance per process. Tables are lazy-loaded on first access.
    """

    def __init__(self):
        self.api_key = os.getenv("AIRTABLE_API_KEY", "")
        self.base_id = os.getenv("AIRTABLE_BASE_ID", "")

        if not self.api_key or self.api_key == "your-airtable-api-key":
            logger.warning("AIRTABLE_API_KEY is not configured. Airtable calls will fail.")
        if not self.base_id or self.base_id == "your-base-id":
            logger.warning("AIRTABLE_BASE_ID is not configured. Airtable calls will fail.")

        self._api = Api(self.api_key)
        self._tables: dict = {}

    def _table(self, table_name: str):
        """Return a cached pyairtable Table object."""
        if table_name not in self._tables:
            self._tables[table_name] = self._api.table(self.base_id, table_name)
        return self._tables[table_name]

    # -----------------------------------------------------------------------
    # Vendors Table
    # -----------------------------------------------------------------------

    def get_vendor_by_name(self, vendor_name: str) -> Optional[dict]:
        """
        Look up a vendor by name (fuzzy: searches Name field).
        Returns the first matching record's fields dict, or None.
        """
        formula = f"SEARCH(LOWER('{vendor_name.lower()}'), LOWER({{Name}}))"
        records = self._table(TABLE_VENDORS).all(formula=formula)
        if records:
            return {"id": records[0]["id"], **records[0]["fields"]}
        return None

    def get_vendor_by_id(self, record_id: str) -> Optional[dict]:
        """Return a vendor record by its Airtable record ID."""
        record = self._table(TABLE_VENDORS).get(record_id)
        if record:
            return {"id": record["id"], **record["fields"]}
        return None

    def update_vendor(self, record_id: str, fields: dict) -> dict:
        """Update vendor fields. Returns updated record."""
        result = self._table(TABLE_VENDORS).update(record_id, fields)
        return {"id": result["id"], **result["fields"]}

    def flag_vendor_refused_compliance(self, record_id: str) -> dict:
        """Permanently flag vendor as having refused Master Insurance Policy."""
        return self.update_vendor(record_id, {
            "Compliance_Status": "Refused Compliance",
            "Refused_Master_Policy": True,
            "Refused_At": datetime.utcnow().isoformat(),
        })

    def get_vendor_compliance_status(self, vendor_id: str, project_id: str) -> dict:
        """
        Return compliance document status for a vendor-project pair.
        Fields checked: W9_Status, Master_Policy_Status, COI_Status_{project}, Indemnity_Status_{project}
        Since Airtable doesn't have dynamic field names, compliance is stored per vendor
        with project-level docs in a linked table or JSON field per project.
        Returns a dict with keys: w9, master_policy, coi, indemnity (each True/False/expired).
        """
        vendor = self.get_vendor_by_id(vendor_id)
        if not vendor:
            return {"w9": False, "master_policy": False, "coi": False, "indemnity": False}

        # These field names must match the Airtable Vendors table schema exactly
        return {
            "w9": vendor.get("W9_On_File", False),
            "master_policy": vendor.get("Master_Policy_On_File", False),
            "coi": vendor.get(f"COI_On_File_{project_id}", False),
            "indemnity": vendor.get(f"Indemnity_On_File_{project_id}", False),
            "refused_compliance": vendor.get("Refused_Master_Policy", False),
        }

    # -----------------------------------------------------------------------
    # Projects Table
    # -----------------------------------------------------------------------

    def get_project_by_name(self, project_name: str) -> Optional[dict]:
        """Look up a project by name."""
        formula = f"SEARCH(LOWER('{project_name.lower()}'), LOWER({{Project_Name}}))"
        records = self._table(TABLE_PROJECTS).all(formula=formula)
        if records:
            return {"id": records[0]["id"], **records[0]["fields"]}
        return None

    def get_project_by_id(self, record_id: str) -> Optional[dict]:
        """Return a project record by Airtable record ID."""
        record = self._table(TABLE_PROJECTS).get(record_id)
        if record:
            return {"id": record["id"], **record["fields"]}
        return None

    def get_all_active_projects(self) -> list[dict]:
        """Return all projects with Status = Active."""
        records = self._table(TABLE_PROJECTS).all(formula="{Status}='Active'")
        return [{"id": r["id"], **r["fields"]} for r in records]

    def get_additional_insured_parties(self, project_id: str) -> list[dict]:
        """Return all required Additional Insured parties for a project."""
        formula = f"FIND('{project_id}', ARRAYJOIN({{Project}}))"
        records = self._table(TABLE_AI_PARTIES).all(formula=formula)
        return [{"id": r["id"], **r["fields"]} for r in records]

    # -----------------------------------------------------------------------
    # Bills Table
    # -----------------------------------------------------------------------

    def get_bill_by_id(self, record_id: str) -> Optional[dict]:
        """Return a bill record by Airtable record ID."""
        record = self._table(TABLE_BILLS).get(record_id)
        if record:
            return {"id": record["id"], **record["fields"]}
        return None

    def get_approved_bills(
        self,
        entity_name: Optional[str] = None,
        as_of: Optional[date] = None,
    ) -> list[dict]:
        """
        Return bills with Approval_Status = Approved and QB_Sync_Status = Pending.
        Optionally filter by entity name and date.
        """
        formula_parts = [
            "{Approval_Status}='Approved'",
            "{QB_Sync_Status}='Pending'",
        ]
        if entity_name:
            formula_parts.append(f"{{Entity_Name}}='{entity_name}'")
        if as_of:
            formula_parts.append(f"IS_BEFORE({{Invoice_Date}}, '{as_of.isoformat()}')")

        formula = "AND(" + ", ".join(formula_parts) + ")"
        records = self._table(TABLE_BILLS).all(formula=formula)
        return [{"id": r["id"], **r["fields"]} for r in records]

    def update_bill(self, record_id: str, fields: dict) -> dict:
        """Update bill fields."""
        result = self._table(TABLE_BILLS).update(record_id, fields)
        return {"id": result["id"], **result["fields"]}

    def update_bill_compliance_status(self, record_id: str, status: str, note: str = "") -> dict:
        """Set the compliance gate status on a bill."""
        return self.update_bill(record_id, {
            "Compliance_Status": status,
            "Compliance_Note": note,
            "Compliance_Checked_At": datetime.utcnow().isoformat(),
        })

    def update_bill_envelope_id(self, record_id: str, envelope_id: str) -> dict:
        """Store the DocuSign envelope ID on a bill."""
        return self.update_bill(record_id, {"DocuSign_Envelope_ID": envelope_id})

    def mark_bill_qb_synced(self, record_id: str) -> dict:
        """Mark a bill as synced to QuickBooks."""
        return self.update_bill(record_id, {
            "QB_Sync_Status": "Synced",
            "QB_Synced_At": datetime.utcnow().isoformat(),
        })

    # -----------------------------------------------------------------------
    # CC Charges Table
    # -----------------------------------------------------------------------

    def create_cc_charge(self, fields: dict) -> dict:
        """Create a new CC charge record."""
        result = self._table(TABLE_CC_CHARGES).create(fields)
        return {"id": result["id"], **result["fields"]}

    def update_cc_charge(self, record_id: str, fields: dict) -> dict:
        """Update a CC charge record."""
        result = self._table(TABLE_CC_CHARGES).update(record_id, fields)
        return {"id": result["id"], **result["fields"]}

    def get_billable_cc_charges_for_month(self, year: int, month: int) -> list[dict]:
        """Return all billable CC charges for a given month (for intercompany reimbursement)."""
        formula = (
            f"AND("
            f"{{Billable_Status}}='Billable',"
            f"YEAR({{Charge_Date}})={year},"
            f"MONTH({{Charge_Date}})={month},"
            f"{{QBO_Push_Status}}='Synced',"
            f"{{Reimbursement_Status}}!='Invoiced'"
            f")"
        )
        records = self._table(TABLE_CC_CHARGES).all(formula=formula)
        return [{"id": r["id"], **r["fields"]} for r in records]

    # -----------------------------------------------------------------------
    # CS Codes Table
    # -----------------------------------------------------------------------

    def get_cs_code(self, cs_code: str) -> Optional[dict]:
        """Return a CS code record by code string."""
        formula = f"{{CS_Code}}='{cs_code}'"
        records = self._table(TABLE_CS_CODES).all(formula=formula)
        if records:
            return {"id": records[0]["id"], **records[0]["fields"]}
        return None

    def get_all_active_cs_codes(self) -> list[dict]:
        """Return all active CS codes (for keyword matching)."""
        records = self._table(TABLE_CS_CODES).all(formula="{Active}=TRUE()")
        return [{"id": r["id"], **r["fields"]} for r in records]

    # -----------------------------------------------------------------------
    # CS-to-GL Mapping Table
    # -----------------------------------------------------------------------

    def get_gl_account(self, cs_code: str, entity_name: str) -> Optional[str]:
        """
        Look up the GL account number for a CS code and entity combination.
        Returns the GL account string, or None if mapping not found.
        """
        formula = (
            f"AND({{CS_Code}}='{cs_code}', {{Entity_Name}}='{entity_name}')"
        )
        records = self._table(TABLE_CS_TO_GL).all(formula=formula)
        if records:
            return records[0]["fields"].get("GL_Account")
        return None

    # -----------------------------------------------------------------------
    # Vendor Coding History
    # -----------------------------------------------------------------------

    def get_vendor_coding_history(self, vendor_id: str, months: int = 12) -> list[dict]:
        """
        Return prior bill line items for a vendor within the last N months.
        Used by cost_coding_agent for historical pattern matching.
        """
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=months * 30)).date().isoformat()
        formula = (
            f"AND("
            f"{{Vendor_ID}}='{vendor_id}',"
            f"IS_AFTER({{Invoice_Date}}, '{cutoff}'),"
            f"{{Approval_Status}}='Approved'"
            f")"
        )
        records = self._table(TABLE_BILLS).all(formula=formula)
        return [{"id": r["id"], **r["fields"]} for r in records]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: Optional[AirtableClient] = None


def get_airtable_client() -> AirtableClient:
    """Return the shared AirtableClient singleton."""
    global _client
    if _client is None:
        _client = AirtableClient()
    return _client
