# agents/extraction_agent.py

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import fitz
from openai import OpenAI
from PIL import Image
import io

from core.config import get_settings
from core.logger import get_logger
from core.models import (
    Invoice,
    IngestionSource,
    InvoiceStatus,
    LineItem,
    VendorInfo,
)
from parsers.pdf_parser import parse_pdf, PDFParseResult

logger = get_logger(__name__)
settings = get_settings()

client = OpenAI(api_key=settings.openai_api_key)

EXTRACTION_PROMPT = """
You are an expert Accounts Payable data extraction engine.

Your task is to extract all invoice fields from the document provided and 
return them as a single valid JSON object. No explanation. No markdown. 
No code fences. Raw JSON only.

Extract the following fields:

{
  "invoice_number": "string or null",
  "invoice_date": "YYYY-MM-DD or null",
  "due_date": "YYYY-MM-DD or null",
  "payment_terms": "string or null",
  "po_number": "string or null",
  "currency": "3-letter ISO code, default USD",
  "subtotal": "numeric string or null",
  "tax": "numeric string or null",
  "total": "numeric string or null",
  "vendor_name": "string or null",
  "vendor_id": "string or null",
  "vendor_email": "string or null",
  "vendor_phone": "string or null",
  "vendor_address": "string or null",
  "vendor_payment_terms": "string or null",
  "line_items": [
    {
      "line_number": integer,
      "description": "string",
      "quantity": "numeric string",
      "unit_price": "numeric string",
      "total": "numeric string"
    }
  ],
  "notes": "string or null",
  "confidence": float between 0.0 and 1.0
}

Rules:
- All monetary values must be plain numeric strings with no currency symbols 
  or commas. Example: "12500.00" not "$12,500.00"
- Dates must be in YYYY-MM-DD format
- If a field is not present in the document, return null
- line_items must be a list even if there is only one item
- confidence reflects how complete and clear the document was
- Return only the JSON object. Nothing else.
"""


def _pdf_page_to_base64(pdf_bytes: bytes, page_num: int = 0) -> str:
    """
    Convert a PDF page to a base64-encoded PNG for GPT-4o vision input.
    Renders at 200 DPI for a balance of quality and token efficiency.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_num]
    mat = fitz.Matrix(200 / 72, 200 / 72)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("png")
    doc.close()
    return base64.b64encode(img_bytes).decode("utf-8")


def _safe_decimal(value: Optional[str]) -> Optional[Decimal]:
    """Convert a string to Decimal safely. Returns None on failure."""
    if value is None:
        return None
    try:
        cleaned = str(value).replace(",", "").replace("$", "").strip()
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _safe_int(value) -> int:
    """Convert a value to int safely. Returns 1 on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _build_line_items(raw_items: list) -> list[LineItem]:
    """Parse raw line item dicts from GPT-4o response into LineItem models."""
    items = []
    for i, item in enumerate(raw_items):
        try:
            line_item = LineItem(
                line_number=_safe_int(item.get("line_number", i + 1)),
                description=str(item.get("description", "")).strip(),
                quantity=_safe_decimal(item.get("quantity", "1")) or Decimal("1"),
                unit_price=_safe_decimal(item.get("unit_price", "0")) or Decimal("0"),
                total=_safe_decimal(item.get("total", "0")) or Decimal("0"),
            )
            items.append(line_item)
        except Exception as e:
            logger.warning("Failed to parse line item", index=i, error=str(e))
    return items


def _parse_gpt_response(raw_json: str, invoice: Invoice) -> Invoice:
    """
    Parse the GPT-4o JSON response and populate the Invoice model.
    Handles missing fields gracefully without raising exceptions.
    """
    try:
        data = json.loads(raw_json.strip())
    except json.JSONDecodeError as e:
        logger.error("GPT-4o returned invalid JSON", error=str(e), raw=raw_json[:500])
        invoice.status = InvoiceStatus.FAILED
        return invoice

    # Vendor
    vendor = VendorInfo(
        vendor_name=data.get("vendor_name") or "Unknown",
        vendor_id=data.get("vendor_id"),
        vendor_email=data.get("vendor_email"),
        vendor_phone=data.get("vendor_phone"),
        vendor_address=data.get("vendor_address"),
        payment_terms=data.get("vendor_payment_terms"),
    )

    # Dates
    from datetime import date

    def parse_date(val: Optional[str]) -> Optional[date]:
        if not val:
            return None
        try:
            return date.fromisoformat(val)
        except ValueError:
            return None

    invoice.invoice_number = data.get("invoice_number")
    invoice.invoice_date = parse_date(data.get("invoice_date"))
    invoice.due_date = parse_date(data.get("due_date"))
    invoice.payment_terms = data.get("payment_terms")
    invoice.po_number = data.get("po_number")
    invoice.currency = data.get("currency") or "USD"
    invoice.subtotal = _safe_decimal(data.get("subtotal"))
    invoice.tax = _safe_decimal(data.get("tax"))
    invoice.total = _safe_decimal(data.get("total"))
    invoice.vendor = vendor
    invoice.notes = data.get("notes")
    invoice.line_items = _build_line_items(data.get("line_items") or [])
    invoice.extraction_confidence = float(data.get("confidence") or 0.0)
    invoice.status = InvoiceStatus.EXTRACTED

    return invoice


def extract_invoice_text_mode(invoice: Invoice, text: str) -> Invoice:
    """
    Send extracted PDF text to GPT-4o for structured field extraction.
    Used when PyMuPDF successfully extracts readable text.
    """
    logger.info("Running text-mode extraction", invoice_id=str(invoice.id))
    invoice.status = InvoiceStatus.EXTRACTING

    prompt = f"{EXTRACTION_PROMPT}\n\nINVOICE TEXT:\n{text}"

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2000,
        )
        raw_json = response.choices[0].message.content
        invoice = _parse_gpt_response(raw_json, invoice)
        invoice.extraction_model = "gpt-4o-text"
        logger.info(
            "Text extraction complete",
            invoice_id=str(invoice.id),
            confidence=invoice.extraction_confidence,
            vendor=invoice.vendor.vendor_name if invoice.vendor else None,
        )

    except Exception as e:
        logger.error("GPT-4o text extraction failed", error=str(e))
        invoice.status = InvoiceStatus.FAILED

    return invoice


def extract_invoice_vision_mode(invoice: Invoice, pdf_bytes: bytes) -> Invoice:
    """
    Send PDF page as image to GPT-4o vision for field extraction.
    Used for scanned PDFs where text extraction is insufficient.
    """
    logger.info("Running vision-mode extraction", invoice_id=str(invoice.id))
    invoice.status = InvoiceStatus.EXTRACTING

    try:
        image_b64 = _pdf_page_to_base64(pdf_bytes)

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": EXTRACTION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_b64}",
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            temperature=0,
            max_tokens=2000,
        )

        raw_json = response.choices[0].message.content
        invoice = _parse_gpt_response(raw_json, invoice)
        invoice.extraction_model = "gpt-4o-vision"
        logger.info(
            "Vision extraction complete",
            invoice_id=str(invoice.id),
            confidence=invoice.extraction_confidence,
        )

    except Exception as e:
        logger.error("GPT-4o vision extraction failed", error=str(e))
        invoice.status = InvoiceStatus.FAILED

    return invoice


def run_extraction_agent(
    invoice: Invoice,
    source: str | bytes | Path,
) -> Invoice:
    """
    Main entry point for the Extraction Agent.

    Accepts a file path or raw PDF bytes.
    Automatically selects text mode or vision mode based on PDF content.
    Returns a populated Invoice model.
    """
    # Load bytes if path given
    if isinstance(source, (str, Path)):
        with open(source, "rb") as f:
            pdf_bytes = f.read()
    else:
        pdf_bytes = source

    # Parse PDF to get text
    parse_result: PDFParseResult = parse_pdf(pdf_bytes)

    if parse_result.success and parse_result.extraction_method == "pymupdf":
        # Clean digital PDF — use fast text mode
        return extract_invoice_text_mode(invoice, parse_result.text)
    else:
        # Scanned or low-quality PDF — use vision mode
        return extract_invoice_vision_mode(invoice, pdf_bytes)
    
    
# ─── Technical Schedule Extraction ───────────────────────────────

SCHEDULE_EXTRACTION_PROMPT = """You are a specialist in reading architectural and construction window/door schedule tables.

Your task is to extract every row from the schedule table on this page and return them as a single valid JSON object.
No explanation. No markdown. No code fences. Raw JSON only.

Extract the following fields for each row:

{
  "schedule_items": [
    {
      "item_type": "window or door",
      "type_mark": "alphanumeric code e.g. W1, D3A or null",
      "size": "width x height string e.g. 900x1200 or null",
      "width_mm": numeric or null,
      "height_mm": numeric or null,
      "quantity": integer or null,
      "location": "room name or area description or null",
      "room_reference": "room number or code or null",
      "elevation_reference": "elevation drawing reference e.g. A-101 or null",
      "finish": "material or finish description or null",
      "frame_type": "frame material e.g. aluminium, timber or null",
      "glazing": "glazing spec e.g. double glazed, clear or null",
      "notes": "any additional notes for this item or null"
    }
  ],
  "schedule_title": "title of the schedule if visible or null",
  "total_items_found": integer,
  "confidence": float between 0.0 and 1.0
}

Rules:
- Extract every row in the table, even if some fields are missing
- size should be the raw string exactly as it appears in the document
- width_mm and height_mm should be parsed numeric values from the size field, in millimetres
- quantity must be an integer, default to 1 if not shown
- confidence reflects how clearly the table was readable and how complete the data is
- Return only the JSON object. Nothing else.
"""

CONFIDENCE_THRESHOLD = 0.5


@dataclass
class ScheduleItem:
    """A single extracted row from a window/door schedule."""
    item_type: Optional[str]
    type_mark: Optional[str]
    size: Optional[str]
    width_mm: Optional[float]
    height_mm: Optional[float]
    quantity: int
    location: Optional[str]
    room_reference: Optional[str]
    elevation_reference: Optional[str]
    finish: Optional[str]
    frame_type: Optional[str]
    glazing: Optional[str]
    notes: Optional[str]


@dataclass
class ScheduleExtractionResult:
    """Result of extracting one schedule page."""
    page_number: int
    schedule_title: Optional[str]
    items: list[ScheduleItem]
    confidence: float
    passed_threshold: bool
    error: Optional[str] = None


def _parse_schedule_response(raw_json: str, page_number: int) -> ScheduleExtractionResult:
    """Parse Claude's JSON response into a ScheduleExtractionResult."""
    import json as _json

    try:
        data = _json.loads(raw_json.strip())
    except _json.JSONDecodeError as e:
        logger.error("Claude returned invalid JSON", page=page_number, error=str(e))
        return ScheduleExtractionResult(
            page_number=page_number,
            schedule_title=None,
            items=[],
            confidence=0.0,
            passed_threshold=False,
            error=f"JSON parse error: {e}",
        )

    confidence = float(data.get("confidence") or 0.0)
    raw_items = data.get("schedule_items") or []

    items = []
    for row in raw_items:
        try:
            item = ScheduleItem(
                item_type=row.get("item_type"),
                type_mark=row.get("type_mark"),
                size=row.get("size"),
                width_mm=float(row["width_mm"]) if row.get("width_mm") is not None else None,
                height_mm=float(row["height_mm"]) if row.get("height_mm") is not None else None,
                quantity=int(row.get("quantity") or 1),
                location=row.get("location"),
                room_reference=row.get("room_reference"),
                elevation_reference=row.get("elevation_reference"),
                finish=row.get("finish"),
                frame_type=row.get("frame_type"),
                glazing=row.get("glazing"),
                notes=row.get("notes"),
            )
            items.append(item)
        except Exception as e:
            logger.warning("Failed to parse schedule row", row=row, error=str(e))

    passed = confidence >= CONFIDENCE_THRESHOLD

    logger.info(
        "Schedule page parsed",
        page=page_number,
        items_found=len(items),
        confidence=confidence,
        passed_threshold=passed,
    )

    return ScheduleExtractionResult(
        page_number=page_number,
        schedule_title=data.get("schedule_title"),
        items=items,
        confidence=confidence,
        passed_threshold=passed,
    )


def extract_schedule_page(pdf_bytes: bytes, page_number: int) -> ScheduleExtractionResult:
    """
    Extract window/door schedule data from a single PDF page using Claude vision.

    Args:
        pdf_bytes:    Raw bytes of the full PDF.
        page_number:  0-indexed page number to extract.

    Returns:
        ScheduleExtractionResult with items list and confidence score.
        Results below CONFIDENCE_THRESHOLD have passed_threshold=False.
    """
    import anthropic as _anthropic

    logger.info("Extracting schedule page", page=page_number)

    image_b64 = _pdf_page_to_base64(pdf_bytes, page_num=page_number)

    anthropic_client = _anthropic.Anthropic(api_key=settings.anthropic_api_key)

    try:
        response = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": SCHEDULE_EXTRACTION_PROMPT,
                        },
                    ],
                }
            ],
        )

        raw_json = response.content[0].text
        return _parse_schedule_response(raw_json, page_number)

    except Exception as e:
        logger.error("Schedule extraction failed", page=page_number, error=str(e))
        return ScheduleExtractionResult(
            page_number=page_number,
            schedule_title=None,
            items=[],
            confidence=0.0,
            passed_threshold=False,
            error=str(e),
        )


def extract_all_schedule_pages(
    pdf_bytes: bytes,
    schedule_page_numbers: list[int],
) -> list[ScheduleExtractionResult]:
    """
    Run schedule extraction across all pages identified by the page classifier.

    Args:
        pdf_bytes:             Raw bytes of the full PDF.
        schedule_page_numbers: List of 0-indexed page numbers to process.

    Returns:
        List of ScheduleExtractionResult, one per page.
        Only results with passed_threshold=True should be exported.
    """
    results = []
    for page_num in schedule_page_numbers:
        result = extract_schedule_page(pdf_bytes, page_num)
        results.append(result)

    passed = [r for r in results if r.passed_threshold]
    logger.info(
        "All schedule pages extracted",
        total_pages=len(results),
        passed_threshold=len(passed),
    )
    return results