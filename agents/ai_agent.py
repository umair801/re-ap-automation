# agents/ai_agent.py
# Claude API prompt wiring for AP Automation System
# Six prompts: invoice OCR, COI verification, project inference,
# GL coding, duplicate detection, and master system prompt

import json
import logging
from typing import Optional
import anthropic
from dotenv import load_dotenv
import os

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Claude client setup
# ---------------------------------------------------------------------------

CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 1000


def _get_client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not configured in .env")
    return anthropic.Anthropic(api_key=api_key)


def _call_claude(system_prompt: str, user_content: str) -> str:
    """
    Make a Claude API call and return the text response.
    Returns empty string on failure.
    """
    try:
        client = _get_client()
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return ""


def _parse_json_response(raw: str) -> dict:
    """Parse JSON from Claude response. Strips markdown fences if present."""
    try:
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        return json.loads(clean.strip())
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Claude JSON response: {e}\nRaw: {raw[:300]}")
        return {}


# ---------------------------------------------------------------------------
# Prompt 1: Invoice OCR / Data Extraction
# ---------------------------------------------------------------------------

INVOICE_EXTRACTION_SYSTEM = """You are an expert accounts payable data extraction engine for a real estate development company.

Extract all invoice fields from the provided text or document and return a single valid JSON object.
No explanation. No markdown. No code fences. Raw JSON only.

Return this exact structure:
{
  "invoice_number": "string or null",
  "invoice_date": "YYYY-MM-DD or null",
  "due_date": "YYYY-MM-DD or null",
  "payment_terms": "string or null",
  "vendor_name": "string or null",
  "vendor_address": "string or null",
  "vendor_email": "string or null",
  "bill_to_entity": "string or null",
  "project_name": "string or null",
  "project_address": "string or null",
  "po_number": "string or null",
  "subtotal": "numeric string or null",
  "tax": "numeric string or null",
  "total": "numeric string or null",
  "line_items": [
    {
      "line_number": integer,
      "description": "string",
      "quantity": "numeric string or null",
      "unit_price": "numeric string or null",
      "amount": "numeric string"
    }
  ],
  "payment_method_indicator": "check or credit_card or null",
  "confidence": float 0.0 to 1.0
}

Rules:
- Monetary values: plain numeric strings, no symbols or commas. "12500.00" not "$12,500.00"
- Dates: YYYY-MM-DD format only
- payment_method_indicator: "credit_card" if document says "charged to card", "receipt", or similar
- confidence: how complete and readable the document was
- Return only JSON. Nothing else."""


def extract_invoice_data(invoice_text: str) -> dict:
    """
    Prompt 1: Extract structured data from invoice text.
    Returns dict with all invoice fields.
    """
    logger.info("Running invoice extraction via Claude API")
    raw = _call_claude(INVOICE_EXTRACTION_SYSTEM, f"INVOICE TEXT:\n{invoice_text}")
    result = _parse_json_response(raw)
    if result:
        logger.info(
            f"Invoice extraction complete | "
            f"vendor={result.get('vendor_name')} | "
            f"total={result.get('total')} | "
            f"confidence={result.get('confidence')}"
        )
    return result


# ---------------------------------------------------------------------------
# Prompt 2: COI Verification
# ---------------------------------------------------------------------------

COI_VERIFICATION_SYSTEM = """You are an expert insurance compliance reviewer for a real estate development company.

Extract all relevant fields from the Certificate of Insurance (COI) document text provided.
Return a single valid JSON object. No explanation. No markdown. Raw JSON only.

Return this exact structure:
{
  "insured_name": "legal name of insured entity or null",
  "insurer_name": "insurance carrier name or null",
  "am_best_rating": "carrier rating e.g. A+ or null",
  "policy_number": "string or null",
  "policy_effective_date": "YYYY-MM-DD or null",
  "policy_expiry_date": "YYYY-MM-DD or null",
  "additional_insured_parties": ["list of all AI party names exactly as written"],
  "certificate_holder": "certificate holder name and address as single string or null",
  "waiver_of_subrogation": true or false,
  "primary_non_contributory": true or false,
  "general_liability_limit": "numeric string or null",
  "auto_liability_limit": "numeric string or null",
  "umbrella_limit": "numeric string or null",
  "workers_comp_limit": "numeric string or null",
  "description_of_operations": "full text of description field or null",
  "acord_form": "25 or 101 or other or null",
  "confidence": float 0.0 to 1.0
}

Rules:
- additional_insured_parties: list every party named as Additional Insured, exactly as written
- waiver_of_subrogation: true only if explicitly stated in the document
- primary_non_contributory: true only if explicitly stated
- Return only JSON. Nothing else."""


def verify_coi_document(coi_text: str) -> dict:
    """
    Prompt 2: Extract and verify COI fields from document text.
    Returns dict with all COI fields for cross-checking against project requirements.
    """
    logger.info("Running COI verification via Claude API")
    raw = _call_claude(COI_VERIFICATION_SYSTEM, f"COI DOCUMENT TEXT:\n{coi_text}")
    result = _parse_json_response(raw)
    if result:
        logger.info(
            f"COI extraction complete | "
            f"insured={result.get('insured_name')} | "
            f"expiry={result.get('policy_expiry_date')} | "
            f"ai_parties={len(result.get('additional_insured_parties', []))}"
        )
    return result


# ---------------------------------------------------------------------------
# Prompt 3: Project Inference
# ---------------------------------------------------------------------------

PROJECT_INFERENCE_SYSTEM = """You are an expert accounts payable analyst for a real estate development company.

Your task is to determine which project a vendor invoice or credit card charge belongs to,
based on available clues in the document text and a list of active projects.

Return a single valid JSON object. No explanation. No markdown. Raw JSON only.

Return this exact structure:
{
  "inferred_project_name": "exact project name from the list or null",
  "confidence": float 0.0 to 1.0,
  "reasoning": "brief explanation of why this project was inferred",
  "is_overhead": true or false,
  "overhead_category": "SaaS/Software, Office Supplies, Marketing, Fuel, Insurance, Other or null"
}

Rules:
- inferred_project_name must exactly match one of the provided project names, or be null
- confidence above 0.8 = clear match, 0.5-0.8 = probable, below 0.5 = uncertain
- is_overhead: true if this is clearly a corporate overhead expense not tied to any project
- If is_overhead is true, inferred_project_name should be null
- Return only JSON. Nothing else."""


def infer_project(document_text: str, active_projects: list[str]) -> dict:
    """
    Prompt 3: Infer which project a document belongs to.
    Returns dict with inferred_project_name and confidence.
    """
    projects_list = "\n".join(f"- {p}" for p in active_projects)
    user_content = (
        f"ACTIVE PROJECTS:\n{projects_list}\n\n"
        f"DOCUMENT TEXT:\n{document_text}"
    )
    logger.info(f"Running project inference via Claude API | {len(active_projects)} active projects")
    raw = _call_claude(PROJECT_INFERENCE_SYSTEM, user_content)
    result = _parse_json_response(raw)
    if result:
        logger.info(
            f"Project inference complete | "
            f"project={result.get('inferred_project_name')} | "
            f"confidence={result.get('confidence')} | "
            f"overhead={result.get('is_overhead')}"
        )
    return result


# ---------------------------------------------------------------------------
# Prompt 4: GL Coding
# ---------------------------------------------------------------------------

GL_CODING_SYSTEM = """You are an expert construction cost accountant for a real estate development company.

Your task is to assign the correct CSI MasterFormat cost code and GL account to each invoice line item.
You will be provided with line item descriptions and a list of available CS codes with their keywords.

Return a single valid JSON object. No explanation. No markdown. Raw JSON only.

Return this exact structure:
{
  "coded_lines": [
    {
      "line_number": integer,
      "description": "original description",
      "suggested_cs_code": "CS code string or null",
      "suggested_cs_description": "human-readable CS description or null",
      "confidence": float 0.0 to 1.0,
      "reasoning": "brief explanation"
    }
  ],
  "overall_confidence": float 0.0 to 1.0,
  "mixed_capitalization_warning": true or false
}

Rules:
- suggested_cs_code must exactly match one of the provided CS codes, or be null
- mixed_capitalization_warning: true if line items mix capitalizable costs (hard/soft cost) with operating expenses
- confidence above 0.9 = auto-proceed, 0.6-0.9 = spot-check, below 0.6 = manual review
- Return only JSON. Nothing else."""


def suggest_gl_coding(
    line_items: list[dict],
    cs_codes: list[dict],
    vendor_name: str,
    project_name: str,
) -> dict:
    """
    Prompt 4: Suggest GL coding for invoice line items.
    Returns dict with coded_lines and confidence scores.
    """
    # Build CS codes reference
    cs_ref = "\n".join(
        f"- {c.get('CS_Code')}: {c.get('CS_Description')} "
        f"[keywords: {c.get('Common_Description_Keywords', '')}]"
        for c in cs_codes[:50]  # Limit to 50 codes to stay within token budget
    )

    lines_text = "\n".join(
        f"Line {item.get('line_number', i+1)}: {item.get('description', '')} "
        f"(${item.get('amount', 0)})"
        for i, item in enumerate(line_items)
    )

    user_content = (
        f"VENDOR: {vendor_name}\n"
        f"PROJECT: {project_name}\n\n"
        f"AVAILABLE CS CODES:\n{cs_ref}\n\n"
        f"LINE ITEMS TO CODE:\n{lines_text}"
    )

    logger.info(f"Running GL coding via Claude API | {len(line_items)} line items")
    raw = _call_claude(GL_CODING_SYSTEM, user_content)
    result = _parse_json_response(raw)
    if result:
        logger.info(
            f"GL coding complete | "
            f"lines={len(result.get('coded_lines', []))} | "
            f"confidence={result.get('overall_confidence')}"
        )
    return result


# ---------------------------------------------------------------------------
# Prompt 5: Duplicate Detection
# ---------------------------------------------------------------------------

DUPLICATE_DETECTION_SYSTEM = """You are an expert accounts payable fraud and duplicate detection analyst.

Your task is to determine if an incoming invoice is a duplicate of any previously processed invoice.
You will be provided with the new invoice details and a list of recent invoices from the same vendor.

Return a single valid JSON object. No explanation. No markdown. Raw JSON only.

Return this exact structure:
{
  "is_duplicate": true or false,
  "confidence": float 0.0 to 1.0,
  "matched_invoice_number": "invoice number of the duplicate if found, or null",
  "match_type": "exact, probable, or none",
  "reasoning": "brief explanation of the duplicate determination"
}

Rules:
- is_duplicate: true only if confidence is above 0.85
- match_type: "exact" if invoice number and amount match, "probable" if likely duplicate, "none" if not duplicate
- Consider: same invoice number, same amount, same date, similar description
- Return only JSON. Nothing else."""


def detect_duplicate(
    new_invoice: dict,
    recent_invoices: list[dict],
) -> dict:
    """
    Prompt 5: Check if an invoice is a duplicate.
    Returns dict with is_duplicate flag and confidence.
    """
    recent_text = "\n".join(
        f"- Invoice #{inv.get('Invoice_Number')} | "
        f"${inv.get('Total_Amount')} | "
        f"{inv.get('Invoice_Date')} | "
        f"{inv.get('Vendor_Name')}"
        for inv in recent_invoices[:20]  # Check last 20 invoices
    )

    user_content = (
        f"NEW INVOICE:\n"
        f"Number: {new_invoice.get('invoice_number')}\n"
        f"Amount: ${new_invoice.get('total')}\n"
        f"Date: {new_invoice.get('invoice_date')}\n"
        f"Vendor: {new_invoice.get('vendor_name')}\n\n"
        f"RECENT INVOICES FROM THIS VENDOR:\n{recent_text}"
    )

    logger.info("Running duplicate detection via Claude API")
    raw = _call_claude(DUPLICATE_DETECTION_SYSTEM, user_content)
    result = _parse_json_response(raw)
    if result:
        logger.info(
            f"Duplicate detection complete | "
            f"is_duplicate={result.get('is_duplicate')} | "
            f"confidence={result.get('confidence')}"
        )
    return result


# ---------------------------------------------------------------------------
# Prompt 6: Master System Prompt (Path Classification)
# ---------------------------------------------------------------------------

PATH_CLASSIFICATION_SYSTEM = """You are the intake classifier for an automated accounts payable system
at a real estate development company.

Your task is to classify an incoming document as either:
1. AP Invoice (Path 1) - requires full compliance gate, payment by check
2. CC Receipt (Path 2) - credit card charge receipt, no compliance gate needed

Return a single valid JSON object. No explanation. No markdown. Raw JSON only.

Return this exact structure:
{
  "path": "AP" or "CC",
  "confidence": float 0.0 to 1.0,
  "reasoning": "brief explanation",
  "indicators": ["list of specific text clues that led to this classification"]
}

Rules:
- "CC" indicators: words like "receipt", "charged to card", "credit card", "card ending in", "transaction"
- "AP" indicators: words like "invoice", "bill", "payment terms", "net 30", "remit to", "please pay"
- When ambiguous, default to "AP" (safer - treating CC as AP triggers unnecessary compliance check,
  but treating AP as CC would skip required compliance documents)
- Return only JSON. Nothing else."""


def classify_document_path(document_text: str) -> dict:
    """
    Prompt 6: Classify document as AP invoice or CC receipt.
    Returns dict with path ("AP" or "CC") and confidence.
    """
    logger.info("Running document path classification via Claude API")
    raw = _call_claude(PATH_CLASSIFICATION_SYSTEM, f"DOCUMENT TEXT:\n{document_text[:2000]}")
    result = _parse_json_response(raw)
    if result:
        logger.info(
            f"Path classification complete | "
            f"path={result.get('path')} | "
            f"confidence={result.get('confidence')}"
        )
    return result


# ---------------------------------------------------------------------------
# Convenience: run all applicable prompts for a new document
# ---------------------------------------------------------------------------

def process_new_document(
    document_text: str,
    active_projects: list[str],
    cs_codes: list[dict],
    recent_invoices: list[dict],
) -> dict:
    """
    Run the full AI analysis pipeline on a new incoming document.
    Returns a combined dict with results from all relevant prompts.
    """
    # Step 1: Classify path
    classification = classify_document_path(document_text)
    path = classification.get("path", "AP")

    # Step 2: Extract invoice data
    extracted = extract_invoice_data(document_text)

    # Step 3: Infer project
    project_inference = infer_project(document_text, active_projects)

    # Step 4: Check for duplicates
    duplicate_check = detect_duplicate(extracted, recent_invoices)

    # Step 5: Suggest GL coding (if AP path and line items exist)
    gl_coding = {}
    if path == "AP" and extracted.get("line_items"):
        gl_coding = suggest_gl_coding(
            line_items=extracted.get("line_items", []),
            cs_codes=cs_codes,
            vendor_name=extracted.get("vendor_name", ""),
            project_name=project_inference.get("inferred_project_name", ""),
        )

    return {
        "path": path,
        "path_confidence": classification.get("confidence", 0),
        "extracted": extracted,
        "project_inference": project_inference,
        "duplicate_check": duplicate_check,
        "gl_coding": gl_coding,
    }
