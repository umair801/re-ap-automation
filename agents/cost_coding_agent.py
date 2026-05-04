# agents/cost_coding_agent.py
# CSI Cost Coding + GL Account Assignment Agent
# Three-signal lookup per line item with confidence levels

import json
import logging
from collections import Counter
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum

from integrations.airtable_client import get_airtable_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums and constants
# ---------------------------------------------------------------------------

class CodingConfidence(str, Enum):
    HIGH = "high"       # Auto-proceed
    MEDIUM = "medium"   # Spot-check flag
    LOW = "low"         # Coding Review queue


class CapitalizationTreatment(str, Enum):
    HARD_COST = "Hard Cost - Capitalize"
    SOFT_COST = "Soft Cost - Capitalize"
    OPERATING = "Operating Expense"
    LOAN = "Loan Origination"
    INVENTORY = "Inventory"


HIGH_CONFIDENCE_THRESHOLD = 0.90   # 90%+ history agreement = HIGH
MEDIUM_CONFIDENCE_THRESHOLD = 0.60  # 60-89% = MEDIUM


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LineItemInput:
    """One line item from an invoice, ready for cost coding."""
    line_number: int
    description: str
    amount: float
    quantity: Optional[float] = None
    unit_price: Optional[float] = None


@dataclass
class CodingResult:
    """Cost coding result for one line item."""
    line_number: int
    description: str
    amount: float
    cs_code: str
    cs_description: str
    gl_account: str
    capitalization_treatment: str
    confidence: CodingConfidence
    signal_used: str            # Which signal produced the result
    needs_review: bool
    review_reason: str = ""
    split_coding_flag: bool = False


@dataclass
class BillCodingResult:
    """Cost coding result for an entire bill."""
    bill_id: str
    vendor_id: str
    entity_name: str
    line_results: list[CodingResult] = field(default_factory=list)
    has_mixed_capitalization: bool = False
    overall_confidence: CodingConfidence = CodingConfidence.LOW
    requires_review: bool = False
    review_reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cost Coding Agent
# ---------------------------------------------------------------------------

class CostCodingAgent:
    """
    Assigns CSI cost codes and GL accounts to invoice line items.

    Three-signal lookup (in priority order):
    1. Vendor coding history (last 12 months) — highest confidence
    2. Line item description keyword match against CS_Codes table
    3. Vendor type default fallback

    Confidence levels:
    - HIGH (90%+): Auto-proceeds to approval
    - MEDIUM (60-89%): Spot-check flag added
    - LOW (<60%): Sent to Coding Review queue
    """

    def __init__(self):
        self.airtable = get_airtable_client()
        self._cs_codes_cache: Optional[list[dict]] = None

    # -----------------------------------------------------------------------
    # Main entry point
    # -----------------------------------------------------------------------

    def code_bill(
        self,
        bill_id: str,
        vendor_id: str,
        entity_name: str,
        project_name: str,
        line_items: list[LineItemInput],
        project_context: Optional[dict] = None,
    ) -> BillCodingResult:
        """
        Run cost coding for all line items on a bill.

        Args:
            bill_id:        Airtable bill record ID.
            vendor_id:      Airtable vendor record ID.
            entity_name:    LLC entity name (for GL account lookup).
            project_name:   Project name (for context override).
            line_items:     List of line items to code.
            project_context: Optional project profile for context override.

        Returns:
            BillCodingResult with coded line items and confidence summary.
        """
        result = BillCodingResult(
            bill_id=bill_id,
            vendor_id=vendor_id,
            entity_name=entity_name,
        )

        # Load vendor coding history once for the whole bill
        history = self._load_vendor_history(vendor_id)

        # Load CS codes once for keyword matching
        cs_codes = self._get_cs_codes()

        # Load vendor record for type default fallback
        vendor = self.airtable.get_vendor_by_id(vendor_id)
        vendor_type = vendor.get("Vendor_Type", "") if vendor else ""
        default_cs_codes = vendor.get("Default_CS_Codes", "") if vendor else ""

        coded_items = []
        for item in line_items:
            coded = self._code_line_item(
                item=item,
                history=history,
                cs_codes=cs_codes,
                entity_name=entity_name,
                vendor_type=vendor_type,
                default_cs_codes=default_cs_codes,
                project_context=project_context,
            )
            coded_items.append(coded)

        result.line_results = coded_items

        # Check for mixed capitalization (flag for split coding)
        treatments = {item.capitalization_treatment for item in coded_items if item.capitalization_treatment}
        capitalizable = {CapitalizationTreatment.HARD_COST, CapitalizationTreatment.SOFT_COST,
                        CapitalizationTreatment.LOAN, CapitalizationTreatment.INVENTORY}
        has_capitalizable = any(t in capitalizable for t in treatments)
        has_operating = CapitalizationTreatment.OPERATING in treatments

        if has_capitalizable and has_operating:
            result.has_mixed_capitalization = True
            result.requires_review = True
            result.review_reasons.append(
                "Bill mixes capitalizable and operating expense items. Split coding required."
            )

        # Set overall confidence to lowest among line items
        confidences = [item.confidence for item in coded_items]
        if CodingConfidence.LOW in confidences:
            result.overall_confidence = CodingConfidence.LOW
            result.requires_review = True
        elif CodingConfidence.MEDIUM in confidences:
            result.overall_confidence = CodingConfidence.MEDIUM
        else:
            result.overall_confidence = CodingConfidence.HIGH

        # Collect review reasons from line items
        for item in coded_items:
            if item.needs_review and item.review_reason:
                result.review_reasons.append(f"Line {item.line_number}: {item.review_reason}")

        logger.info(
            f"Bill {bill_id} coded: {len(coded_items)} lines | "
            f"Confidence: {result.overall_confidence} | "
            f"Needs review: {result.requires_review}"
        )

        return result

    # -----------------------------------------------------------------------
    # Line item coding
    # -----------------------------------------------------------------------

    def _code_line_item(
        self,
        item: LineItemInput,
        history: dict,
        cs_codes: list[dict],
        entity_name: str,
        vendor_type: str,
        default_cs_codes: str,
        project_context: Optional[dict],
    ) -> CodingResult:
        """
        Code a single line item using the three-signal approach.
        Returns a CodingResult with the best CS code and GL account.
        """

        # Signal 1: Vendor history (highest confidence)
        history_result = self._signal_vendor_history(history, item.description)

        # Signal 2: Description keyword match
        keyword_result = self._signal_keyword_match(item.description, cs_codes)

        # Signal 3: Vendor type default
        default_result = self._signal_vendor_default(default_cs_codes, cs_codes)

        # Reconcile signals
        cs_code, confidence, signal_used = self._reconcile_signals(
            history_result, keyword_result, default_result
        )

        # Project context override
        if project_context:
            override = self._apply_project_context(cs_code, item.description, project_context)
            if override:
                cs_code = override
                signal_used = "project_context_override"

        # GL account lookup
        gl_account = ""
        cs_description = ""
        capitalization = ""

        if cs_code:
            gl_account = self.airtable.get_gl_account(cs_code, entity_name) or ""
            cs_record = self._get_cs_code_record(cs_code, cs_codes)
            if cs_record:
                cs_description = cs_record.get("CS_Description", "")
                capitalization = cs_record.get("Capitalization_Treatment", "")

        # Determine if review is needed
        needs_review = False
        review_reason = ""

        if not cs_code:
            needs_review = True
            review_reason = "No CS code could be assigned. Manual coding required."
            confidence = CodingConfidence.LOW

        if not gl_account and cs_code:
            needs_review = True
            review_reason = f"GL account mapping missing for CS code {cs_code} + entity {entity_name}."

        if confidence == CodingConfidence.MEDIUM:
            needs_review = True
            review_reason = review_reason or "Medium confidence coding. Spot-check recommended."

        if confidence == CodingConfidence.LOW:
            needs_review = True
            review_reason = review_reason or "Low confidence coding. Manual review required."

        return CodingResult(
            line_number=item.line_number,
            description=item.description,
            amount=item.amount,
            cs_code=cs_code or "UNKNOWN",
            cs_description=cs_description,
            gl_account=gl_account,
            capitalization_treatment=capitalization,
            confidence=confidence,
            signal_used=signal_used,
            needs_review=needs_review,
            review_reason=review_reason,
        )

    # -----------------------------------------------------------------------
    # Signal 1: Vendor history
    # -----------------------------------------------------------------------

    def _load_vendor_history(self, vendor_id: str) -> dict:
        """
        Load vendor coding history from Airtable (last 12 months).
        Returns dict: {cs_code: count} representing coding frequency.
        """
        try:
            records = self.airtable.get_vendor_coding_history(vendor_id, months=12)
            code_counts: Counter = Counter()

            for record in records:
                line_items_json = record.get("Line_Items_JSON", "")
                if line_items_json:
                    try:
                        items = json.loads(line_items_json)
                        for item in items:
                            cs_code = item.get("cs_code")
                            if cs_code:
                                code_counts[cs_code] += 1
                    except (json.JSONDecodeError, TypeError):
                        pass

            return dict(code_counts)
        except Exception as e:
            logger.warning(f"Could not load vendor history: {e}")
            return {}

    def _signal_vendor_history(
        self, history: dict, description: str
    ) -> tuple[str, float]:
        """
        Return (cs_code, confidence_ratio) from vendor history.
        confidence_ratio = count of top code / total codes seen.
        """
        if not history:
            return ("", 0.0)

        total = sum(history.values())
        if total == 0:
            return ("", 0.0)

        top_code = max(history, key=history.get)
        ratio = history[top_code] / total
        return (top_code, ratio)

    # -----------------------------------------------------------------------
    # Signal 2: Keyword match
    # -----------------------------------------------------------------------

    def _get_cs_codes(self) -> list[dict]:
        """Return cached CS codes list."""
        if self._cs_codes_cache is None:
            try:
                self._cs_codes_cache = self.airtable.get_all_active_cs_codes()
            except Exception as e:
                logger.warning(f"Could not load CS codes: {e}")
                self._cs_codes_cache = []
        return self._cs_codes_cache

    def _signal_keyword_match(
        self, description: str, cs_codes: list[dict]
    ) -> tuple[str, float]:
        """
        Match line item description against CS code keywords.
        Returns (cs_code, confidence_score) where score = matched_keywords / total_keywords.
        """
        if not description or not cs_codes:
            return ("", 0.0)

        description_lower = description.lower()
        best_code = ""
        best_score = 0.0

        for cs_record in cs_codes:
            keywords_raw = cs_record.get("Common_Description_Keywords", "")
            if not keywords_raw:
                continue

            keywords = [k.strip().lower() for k in keywords_raw.replace(",", "\n").splitlines() if k.strip()]
            if not keywords:
                continue

            matches = sum(1 for kw in keywords if kw in description_lower)
            if matches > 0:
                score = matches / len(keywords)
                if score > best_score:
                    best_score = score
                    best_code = cs_record.get("CS_Code", "")

        return (best_code, min(best_score, 0.85))  # Cap keyword confidence at 0.85

    # -----------------------------------------------------------------------
    # Signal 3: Vendor type default
    # -----------------------------------------------------------------------

    def _signal_vendor_default(
        self, default_cs_codes: str, cs_codes: list[dict]
    ) -> tuple[str, float]:
        """
        Return the first default CS code from the vendor record.
        Confidence is always LOW (0.4) — this is a fallback only.
        """
        if not default_cs_codes:
            return ("", 0.0)

        first_code = default_cs_codes.split(",")[0].strip()
        if first_code:
            return (first_code, 0.40)
        return ("", 0.0)

    # -----------------------------------------------------------------------
    # Signal reconciliation
    # -----------------------------------------------------------------------

    def _reconcile_signals(
        self,
        history_result: tuple,
        keyword_result: tuple,
        default_result: tuple,
    ) -> tuple[str, CodingConfidence, str]:
        """
        Reconcile three signals into a final CS code and confidence level.

        Priority:
        1. If history signal >= HIGH_CONFIDENCE_THRESHOLD: use history, HIGH confidence
        2. If history and keyword agree: HIGH confidence
        3. If history and keyword disagree but both present: use history, MEDIUM confidence
        4. If only keyword present: MEDIUM confidence
        5. If only default present: LOW confidence
        6. If nothing: LOW confidence, no code
        """
        h_code, h_score = history_result
        k_code, k_score = keyword_result
        d_code, d_score = default_result

        # History alone is highly confident
        if h_code and h_score >= HIGH_CONFIDENCE_THRESHOLD:
            return (h_code, CodingConfidence.HIGH, "vendor_history")

        # History and keyword agree
        if h_code and k_code and h_code == k_code:
            return (h_code, CodingConfidence.HIGH, "history_keyword_agree")

        # History exists but moderate confidence
        if h_code and h_score >= MEDIUM_CONFIDENCE_THRESHOLD:
            return (h_code, CodingConfidence.MEDIUM, "vendor_history_medium")

        # Keyword match only
        if k_code and k_score > 0:
            confidence = CodingConfidence.MEDIUM if k_score >= 0.5 else CodingConfidence.LOW
            return (k_code, confidence, "keyword_match")

        # History exists but low confidence
        if h_code:
            return (h_code, CodingConfidence.LOW, "vendor_history_low")

        # Default fallback
        if d_code:
            return (d_code, CodingConfidence.LOW, "vendor_type_default")

        return ("", CodingConfidence.LOW, "no_signal")

    # -----------------------------------------------------------------------
    # Project context override
    # -----------------------------------------------------------------------

    def _apply_project_context(
        self, suggested_code: str, description: str, project_context: dict
    ) -> Optional[str]:
        """
        Apply project-specific coding overrides.
        Example: concrete on a foundation pour vs. sidewalk repair.

        Returns override CS code if applicable, else None.
        """
        overrides = project_context.get("cs_code_overrides", {})
        description_lower = description.lower()

        for keyword, override_code in overrides.items():
            if keyword.lower() in description_lower:
                logger.info(
                    f"Project context override applied: '{keyword}' -> {override_code} "
                    f"(was: {suggested_code})"
                )
                return override_code

        return None

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _get_cs_code_record(self, cs_code: str, cs_codes: list[dict]) -> Optional[dict]:
        """Find a CS code record from the cached list."""
        for record in cs_codes:
            if record.get("CS_Code") == cs_code:
                return record
        return None

    def invalidate_cs_codes_cache(self):
        """Force reload of CS codes on next call (use after CS codes table update)."""
        self._cs_codes_cache = None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_agent: Optional[CostCodingAgent] = None


def get_cost_coding_agent() -> CostCodingAgent:
    """Return the shared CostCodingAgent singleton."""
    global _agent
    if _agent is None:
        _agent = CostCodingAgent()
    return _agent
