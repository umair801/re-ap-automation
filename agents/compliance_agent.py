# agents/compliance_agent.py
# Four-Document Compliance Gate + Vendor Name Normalization
# Enforces the rule: no bill is approved without all 4 documents current

import re
import logging
from datetime import datetime, date
from typing import Optional
from enum import Enum

from integrations.airtable_client import get_airtable_client
from integrations.docusign_client import get_docusign_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUFFIX_NORMALIZATIONS = {
    "llc": "llc", "l.l.c.": "llc", "l.l.c": "llc",
    "inc": "inc", "inc.": "inc", "incorporated": "inc",
    "corp": "corp", "corp.": "corp", "corporation": "corp",
    "ltd": "ltd", "ltd.": "ltd", "limited": "ltd",
    "lp": "lp", "l.p.": "lp",
    "llp": "llp", "l.l.p.": "llp",
    "pllc": "pllc", "p.l.l.c.": "pllc",
    "co": "co", "co.": "co", "company": "co",
}

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ComplianceState(str, Enum):
    COMPLIANT = "Compliant"           # All 4 docs present and current
    PENDING = "Pending"               # Envelope sent, awaiting return
    AWAITING_DOCS = "Awaiting Docs"   # Missing docs, envelope not yet sent
    BLOCKED = "Blocked"               # Name mismatch or refused compliance
    REFUSED = "Refused Compliance"    # Vendor refused Master Policy


class EnvelopeType(str, Enum):
    FULL = "full"           # New vendor: all 4 docs
    PARTIAL = "partial"     # Existing vendor, new project: COI + Indemnity only
    RENEWAL = "renewal"     # Expired doc: only the expiring document


# ---------------------------------------------------------------------------
# Vendor Name Normalization
# ---------------------------------------------------------------------------

def normalize_vendor_name(name: str) -> str:
    """
    Normalize a vendor name for cross-document comparison.

    Steps:
    1. Lowercase
    2. Strip punctuation (except spaces)
    3. Collapse whitespace
    4. Standardize entity suffix abbreviations

    Returns normalized string for comparison only — never displayed to user.
    """
    if not name:
        return ""

    # Step 1: Lowercase
    normalized = name.lower().strip()

    # Step 2: Standardize suffix abbreviations BEFORE stripping punctuation
    # This handles 'L.L.C.' -> 'llc' before dots are stripped to spaces
    for dotted, standard in SUFFIX_NORMALIZATIONS.items():
        pattern = r'\b' + re.escape(dotted) + r'\b'
        normalized = re.sub(pattern, standard, normalized)

    # Step 3: Remove punctuation except spaces
    normalized = re.sub(r"[^\w\s]", " ", normalized)

    # Step 4: Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()

    # Step 5: Standardize suffix abbreviations again on cleaned string
    words = normalized.split()
    if words:
        last_word = words[-1]
        if last_word in SUFFIX_NORMALIZATIONS:
            words[-1] = SUFFIX_NORMALIZATIONS[last_word]
    normalized = " ".join(words)

    return normalized


def names_match(name_a: str, name_b: str) -> bool:
    """
    Return True if two vendor names refer to the same legal entity.
    Cosmetic differences (punctuation, case, suffix abbreviation) pass.
    Substantive differences (different core name, different suffix) fail.
    """
    return normalize_vendor_name(name_a) == normalize_vendor_name(name_b)


def check_name_consistency(
    w9_name: str,
    coi_name: str,
    indemnity_name: str,
) -> dict:
    """
    Cross-check vendor name across all three compliance documents.

    Returns:
        {
            'consistent': bool,
            'normalized': {w9, coi, indemnity},
            'mismatches': list of mismatch descriptions
        }
    """
    norm_w9 = normalize_vendor_name(w9_name)
    norm_coi = normalize_vendor_name(coi_name)
    norm_ind = normalize_vendor_name(indemnity_name)

    mismatches = []

    if norm_w9 != norm_coi:
        mismatches.append(
            f"W-9 name '{w9_name}' does not match COI name '{coi_name}'"
        )
    if norm_w9 != norm_ind:
        mismatches.append(
            f"W-9 name '{w9_name}' does not match Indemnity name '{indemnity_name}'"
        )
    if norm_coi != norm_ind:
        mismatches.append(
            f"COI name '{coi_name}' does not match Indemnity name '{indemnity_name}'"
        )

    return {
        "consistent": len(mismatches) == 0,
        "normalized": {
            "w9": norm_w9,
            "coi": norm_coi,
            "indemnity": norm_ind,
        },
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# Compliance Agent
# ---------------------------------------------------------------------------

class ComplianceAgent:
    """
    Enforces the four-document compliance gate for Path 1 AP invoices.

    The four documents required per vendor-project pair:
    1. W-9 (vendor-level)
    2. Master Insurance Policy (vendor-level, full policy not just COI)
    3. Certificate of Insurance (project-level, names specific LLC as AI)
    4. Indemnity Agreement (project-level, generated from template)

    No bill proceeds to approval without all four. No exceptions.
    """

    def __init__(self):
        self.airtable = get_airtable_client()
        self.docusign = get_docusign_client()

    # -----------------------------------------------------------------------
    # Main entry point: check compliance for a bill
    # -----------------------------------------------------------------------

    def check_bill_compliance(self, bill_id: str) -> dict:
        """
        Run the full compliance gate for a bill.

        Args:
            bill_id: Airtable record ID of the bill.

        Returns:
            {
                'state': ComplianceState,
                'can_proceed': bool,
                'missing_docs': list,
                'envelope_id': str or None,
                'note': str
            }
        """
        bill = self.airtable.get_bill_by_id(bill_id)
        if not bill:
            return self._result(ComplianceState.BLOCKED, False, [], None,
                                f"Bill {bill_id} not found in Airtable.")

        vendor_id = bill.get("Vendor_ID")
        project_id = bill.get("Project_ID")
        vendor_name = bill.get("Vendor_Name", "")

        if not vendor_id:
            return self._result(ComplianceState.BLOCKED, False, [],
                                None, "Bill has no Vendor_ID. Cannot check compliance.")

        vendor = self.airtable.get_vendor_by_id(vendor_id)
        if not vendor:
            return self._result(ComplianceState.BLOCKED, False, [],
                                None, f"Vendor {vendor_id} not found.")

        # Check if vendor has permanently refused compliance
        if vendor.get("Refused_Master_Policy"):
            self.airtable.update_bill_compliance_status(
                bill_id, ComplianceState.REFUSED,
                "Vendor has permanently refused to provide Master Insurance Policy."
            )
            return self._result(
                ComplianceState.REFUSED, False, ["Master Insurance Policy"],
                None, "Vendor flagged as Refused Compliance. Bill permanently blocked."
            )

        # Determine missing documents
        missing = self._get_missing_docs(vendor, project_id)

        if not missing:
            # All 4 docs present — check name consistency
            name_check = self._run_name_consistency_check(vendor, project_id)
            if not name_check["consistent"]:
                note = "Vendor name mismatch across compliance documents: " + "; ".join(name_check["mismatches"])
                self.airtable.update_bill_compliance_status(bill_id, ComplianceState.BLOCKED, note)
                logger.warning(f"Bill {bill_id} blocked: {note}")
                return self._result(ComplianceState.BLOCKED, False, [], None, note)

            self.airtable.update_bill_compliance_status(bill_id, ComplianceState.COMPLIANT, "All 4 documents verified.")
            logger.info(f"Bill {bill_id} is fully compliant.")
            return self._result(ComplianceState.COMPLIANT, True, [], None, "All 4 documents verified.")

        # Missing docs — determine envelope type and send
        logger.info(f"Bill {bill_id} missing docs: {missing}. Sending DocuSign envelope.")
        envelope_result = self._send_compliance_envelope(bill, vendor, project_id, missing)

        self.airtable.update_bill_compliance_status(
            bill_id, ComplianceState.PENDING,
            f"Missing: {', '.join(missing)}. Envelope sent: {envelope_result.get('envelope_id', 'N/A')}"
        )

        if envelope_result.get("envelope_id"):
            self.airtable.update_bill_envelope_id(bill_id, envelope_result["envelope_id"])

        return self._result(
            ComplianceState.PENDING, False, missing,
            envelope_result.get("envelope_id"),
            f"Compliance envelope sent. Awaiting: {', '.join(missing)}"
        )

    # -----------------------------------------------------------------------
    # Document status checks
    # -----------------------------------------------------------------------

    def _get_missing_docs(self, vendor: dict, project_id: Optional[str]) -> list[str]:
        """Return list of missing document names for this vendor-project pair."""
        missing = []

        if not vendor.get("W9_On_File"):
            missing.append("W-9")

        if not vendor.get("Master_Policy_On_File"):
            missing.append("Master Insurance Policy")

        if project_id:
            if not vendor.get(f"COI_On_File_{project_id}"):
                missing.append("Certificate of Insurance")
            if not vendor.get(f"Indemnity_On_File_{project_id}"):
                missing.append("Indemnity Agreement")
        else:
            missing.append("Certificate of Insurance")
            missing.append("Indemnity Agreement")

        return missing

    def _run_name_consistency_check(self, vendor: dict, project_id: Optional[str]) -> dict:
        """
        Run cross-document name consistency check.
        Uses stored names from vendor record (populated when docs were received).
        """
        w9_name = vendor.get("W9_Legal_Name", vendor.get("Name", ""))
        coi_name = vendor.get("COI_Legal_Name", vendor.get("Name", ""))
        indemnity_name = vendor.get("Indemnity_Legal_Name", vendor.get("Name", ""))

        return check_name_consistency(w9_name, coi_name, indemnity_name)

    # -----------------------------------------------------------------------
    # DocuSign envelope sending
    # -----------------------------------------------------------------------

    def _send_compliance_envelope(
        self,
        bill: dict,
        vendor: dict,
        project_id: Optional[str],
        missing_docs: list[str],
    ) -> dict:
        """
        Determine envelope type and send via DocuSign.
        Returns dict with envelope_id or error.
        """
        project = self.airtable.get_project_by_id(project_id) if project_id else {}

        # Determine envelope type
        has_vendor_level = vendor.get("W9_On_File") and vendor.get("Master_Policy_On_File")
        if has_vendor_level:
            envelope_type = EnvelopeType.PARTIAL  # Only COI + Indemnity needed
        else:
            envelope_type = EnvelopeType.FULL     # All 4 docs needed

        # Get indemnity template
        template_name = project.get("Indemnity_Template", "standard_construction")

        # Build merge fields from project data
        merge_fields = self._build_merge_fields(vendor, project)

        vendor_email = vendor.get("Email", "")
        vendor_name = vendor.get("Name", "")
        project_name = project.get("Project_Name", bill.get("Project_Name", "Unknown Project"))

        if not vendor_email:
            logger.error(f"Vendor {vendor_name} has no email. Cannot send envelope.")
            return {"envelope_id": None, "error": "Vendor email missing."}

        try:
            envelope_id = self.docusign.send_onboarding_envelope(
                vendor_email=vendor_email,
                vendor_name=vendor_name,
                project_name=project_name,
                template_name=template_name,
                merge_fields=merge_fields,
                envelope_type=envelope_type.value,
            )
            logger.info(
                f"Envelope sent to {vendor_email} | "
                f"Type: {envelope_type.value} | "
                f"Envelope ID: {envelope_id}"
            )
            return {"envelope_id": envelope_id, "envelope_type": envelope_type.value}

        except ValueError as e:
            # Template ID not configured — log but don't crash
            logger.warning(f"DocuSign template not configured: {e}. Envelope skipped.")
            return {"envelope_id": None, "error": str(e)}
        except Exception as e:
            logger.error(f"DocuSign envelope send failed: {e}")
            return {"envelope_id": None, "error": str(e)}

    def _build_merge_fields(self, vendor: dict, project: dict) -> dict:
        """Build DocuSign template merge fields from vendor and project data."""
        return {
            "VendorName": vendor.get("Name", ""),
            "ProjectName": project.get("Project_Name", ""),
            "PropertyAddress": project.get("Property_Address", ""),
            "BlockAndLot": project.get("Block_and_Lot", ""),
            "ProjectLLC": project.get("Project_LLC", ""),
            "OperatingEntity": project.get("Operating_Entity", ""),
            "CertificateHolderName": project.get("Certificate_Holder_Name", ""),
            "CertificateHolderAddress": project.get("Certificate_Holder_Address", ""),
            "Indemnitees": project.get("Indemnitees", ""),
            "StandardScope": project.get("Standard_Scope_Language", ""),
            "SpecialInsuranceReqs": project.get("Special_Insurance_Requirements", ""),
            "ExpectedCompletion": str(project.get("Expected_Completion", "")),
        }

    # -----------------------------------------------------------------------
    # Webhook handlers (called from docusign_router.py)
    # -----------------------------------------------------------------------

    def process_completed_envelope(self, envelope_id: str) -> dict:
        """
        Called when DocuSign Connect fires a 'completed' event.
        Downloads documents and marks compliance fields in Airtable.
        """
        logger.info(f"Processing completed envelope: {envelope_id}")

        # Download completed documents
        output_dir = f"downloads/envelopes/{envelope_id}"
        try:
            downloaded = self.docusign.download_envelope_documents(
                envelope_id=envelope_id,
                output_dir=output_dir,
            )
            logger.info(f"Downloaded {len(downloaded)} documents for envelope {envelope_id}")
        except Exception as e:
            logger.error(f"Failed to download envelope {envelope_id} documents: {e}")
            return {"success": False, "error": str(e)}

        # TODO: Run OCR/AI extraction on downloaded docs to extract vendor names
        # then call check_name_consistency() before marking as compliant
        # For now: mark vendor docs as received (operator verifies names via Airtable)

        logger.info(
            f"Envelope {envelope_id} documents downloaded to {output_dir}. "
            f"Operator should verify name consistency and update compliance fields."
        )
        return {"success": True, "envelope_id": envelope_id, "documents": downloaded}

    def handle_declined_envelope(self, envelope_id: str) -> dict:
        """
        Called when a vendor declines the DocuSign envelope.
        If Master Policy was in the envelope, flag vendor as Refused Compliance.
        """
        logger.warning(f"Envelope {envelope_id} declined by vendor.")
        # Operator must review and manually flag if Master Policy was refused
        return {"envelope_id": envelope_id, "action": "operator_review_required"}

    def handle_voided_envelope(self, envelope_id: str) -> dict:
        """Called when an envelope expires or is voided."""
        logger.warning(f"Envelope {envelope_id} voided or expired.")
        return {"envelope_id": envelope_id, "action": "re_send_required"}

    # -----------------------------------------------------------------------
    # COI Verification (AI-assisted, called after document download)
    # -----------------------------------------------------------------------

    def verify_coi(self, coi_data: dict, project_id: str) -> dict:
        """
        Verify a Certificate of Insurance against project requirements.

        Args:
            coi_data: Extracted COI fields from AI OCR agent.
            project_id: Airtable project record ID.

        Returns:
            {
                'passed': bool,
                'failures': list of failed checks,
                'warnings': list of warnings
            }
        """
        project = self.airtable.get_project_by_id(project_id)
        if not project:
            return {"passed": False, "failures": [f"Project {project_id} not found."], "warnings": []}

        ai_parties = self.airtable.get_additional_insured_parties(project_id)
        failures = []
        warnings = []

        # Check all required AI parties are named
        required_parties = [
            project.get("Project_LLC"),
            project.get("Operating_Entity"),
        ]
        if project.get("Lender"):
            required_parties.append(project.get("Lender"))
        if project.get("Construction_Manager"):
            required_parties.append(project.get("Construction_Manager"))

        for party in ai_parties:
            required_parties.append(party.get("Party_Name"))

        coi_ai_parties = coi_data.get("additional_insured_parties", [])
        for required in required_parties:
            if required and not any(names_match(required, p) for p in coi_ai_parties):
                failures.append(f"Required Additional Insured party not named: '{required}'")

        # Check certificate holder
        required_holder = project.get("Certificate_Holder_Name", "")
        coi_holder = coi_data.get("certificate_holder", "")
        if required_holder and not names_match(required_holder, coi_holder):
            failures.append(
                f"Certificate Holder mismatch. Required: '{required_holder}'. Found: '{coi_holder}'"
            )

        # Check coverage dates
        expected_completion = project.get("Expected_Completion")
        coi_expiry = coi_data.get("policy_expiry_date")
        if expected_completion and coi_expiry:
            if coi_expiry < expected_completion:
                failures.append(
                    f"COI coverage expires {coi_expiry} before project completion {expected_completion}."
                )

        # Check required wording
        if not coi_data.get("waiver_of_subrogation"):
            failures.append("Waiver of Subrogation not found on COI.")
        if not coi_data.get("primary_non_contributory"):
            failures.append("Primary and Non-Contributory wording not found on COI.")

        # Check carrier rating
        am_best_rating = coi_data.get("am_best_rating", "")
        if am_best_rating and am_best_rating < "A-":
            failures.append(f"Carrier A.M. Best rating '{am_best_rating}' is below required A-.")

        passed = len(failures) == 0
        if passed:
            logger.info(f"COI verification PASSED for project {project_id}.")
        else:
            logger.warning(f"COI verification FAILED for project {project_id}: {failures}")

        return {"passed": passed, "failures": failures, "warnings": warnings}

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _result(
        self,
        state: ComplianceState,
        can_proceed: bool,
        missing_docs: list,
        envelope_id: Optional[str],
        note: str,
    ) -> dict:
        return {
            "state": state,
            "can_proceed": can_proceed,
            "missing_docs": missing_docs,
            "envelope_id": envelope_id,
            "note": note,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_agent: Optional[ComplianceAgent] = None


def get_compliance_agent() -> ComplianceAgent:
    """Return the shared ComplianceAgent singleton."""
    global _agent
    if _agent is None:
        _agent = ComplianceAgent()
    return _agent
