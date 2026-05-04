# agents/page_classifier.py

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from anthropic import Anthropic

from core.config import get_settings
from core.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

PAGE_TYPES = ["schedule", "floor_plan", "elevation", "cover", "notes"]

CLASSIFIER_PROMPT = """You are a technical document page classifier.

Examine this page image and classify it into exactly one of these categories:
- schedule: A table or spreadsheet listing items such as windows, doors, hardware, finishes, quantities, or room references. This is the primary data page type.
- floor_plan: A top-down architectural drawing showing room layouts, walls, and dimensions.
- elevation: A side-view architectural drawing showing building facades or interior wall views.
- cover: A title page, cover sheet, project summary, or signature block with no schedule data.
- notes: General specifications, written notes, legends, abbreviations, or annotation pages.

Respond with a single valid JSON object only. No explanation. No markdown. No code fences.

{
  "page_type": "<one of: schedule, floor_plan, elevation, cover, notes>",
  "confidence": <float between 0.0 and 1.0>,
  "reason": "<one sentence explaining the classification>"
}"""


@dataclass
class PageClassification:
    page_number: int          # 1-based
    page_type: str
    confidence: float
    reason: str
    included: bool            # True if page_type == "schedule"


@dataclass
class ClassifierResult:
    pdf_path: str
    total_pages: int
    classifications: list[PageClassification] = field(default_factory=list)
    schedule_pages: list[int] = field(default_factory=list)   # 1-based page numbers
    error: Optional[str] = None


def _render_page_to_base64(page: fitz.Page, dpi: int = 150) -> str:
    """Render a PDF page to a PNG image and return as base64 string."""
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img_bytes = pix.tobytes("png")
    return base64.standard_b64encode(img_bytes).decode("utf-8")


def _classify_page_image(client: Anthropic, image_b64: str, page_number: int) -> PageClassification:
    """Send a single page image to Claude and return the classification."""
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=256,
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
                            "text": CLASSIFIER_PROMPT,
                        },
                    ],
                }
            ],
        )

        raw = response.content[0].text.strip()
        parsed = json.loads(raw)

        page_type = parsed.get("page_type", "notes")
        if page_type not in PAGE_TYPES:
            page_type = "notes"

        confidence = float(parsed.get("confidence", 0.0))
        reason = parsed.get("reason", "")

        return PageClassification(
            page_number=page_number,
            page_type=page_type,
            confidence=confidence,
            reason=reason,
            included=(page_type == "schedule"),
        )

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning("Page %d classification parse error: %s", page_number, e)
        return PageClassification(
            page_number=page_number,
            page_type="notes",
            confidence=0.0,
            reason="Parse error during classification",
            included=False,
        )
    except Exception as e:
        logger.error("Page %d classification failed: %s", page_number, e)
        return PageClassification(
            page_number=page_number,
            page_type="notes",
            confidence=0.0,
            reason=f"API error: {e}",
            included=False,
        )


def classify_pdf_pages(pdf_path: str | Path, dpi: int = 150) -> ClassifierResult:
    """
    Classify every page in the PDF and return a ClassifierResult.
    Only pages classified as 'schedule' are marked included=True.

    Args:
        pdf_path: Path to the PDF file.
        dpi: Rendering resolution. 150 dpi is sufficient for classification.

    Returns:
        ClassifierResult with per-page classifications and filtered schedule_pages list.
    """
    pdf_path = Path(pdf_path)
    result = ClassifierResult(pdf_path=str(pdf_path), total_pages=0)

    if not pdf_path.exists():
        result.error = f"File not found: {pdf_path}"
        logger.error(result.error)
        return result

    anthropic_client = Anthropic(api_key=settings.anthropic_api_key)

    try:
        doc = fitz.open(str(pdf_path))
        result.total_pages = len(doc)
        logger.info("Classifying %d pages in: %s", result.total_pages, pdf_path.name)

        for i, page in enumerate(doc):
            page_number = i + 1
            logger.debug("Classifying page %d / %d", page_number, result.total_pages)

            image_b64 = _render_page_to_base64(page, dpi=dpi)
            classification = _classify_page_image(anthropic_client, image_b64, page_number)
            result.classifications.append(classification)

            if classification.included:
                result.schedule_pages.append(page_number)
                logger.info(
                    "Page %d: SCHEDULE (confidence=%.2f) — %s",
                    page_number, classification.confidence, classification.reason,
                )
            else:
                logger.info(
                    "Page %d: %s (confidence=%.2f, skipped)",
                    page_number, classification.page_type.upper(), classification.confidence,
                )

        doc.close()

    except Exception as e:
        result.error = f"PDF processing error: {e}"
        logger.error(result.error)

    logger.info(
        "Classification complete. %d of %d pages are schedule pages.",
        len(result.schedule_pages), result.total_pages,
    )
    return result


# ─── Router Adapter ───────────────────────────────────────────────────────────

@dataclass
class RouterClassifierResult:
    """Flat result shape expected by extraction_router.py."""
    success: bool
    total_pages: int
    schedule_pages: list[int]      # 0-indexed page numbers for extraction agent
    has_schedules: bool
    error: Optional[str] = None


def run_page_classifier(pdf_bytes: bytes) -> RouterClassifierResult:
    """
    Adapter used by the FastAPI extraction router.
    Accepts raw PDF bytes, runs classify_pdf_pages, and returns a
    RouterClassifierResult with 0-indexed schedule_pages for the extraction agent.
    """
    import tempfile, os

    # Write bytes to a temp file so classify_pdf_pages can open it
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name

    try:
        result = classify_pdf_pages(tmp_path)
    finally:
        os.unlink(tmp_path)

    if result.error and result.total_pages == 0:
        return RouterClassifierResult(
            success=False,
            total_pages=0,
            schedule_pages=[],
            has_schedules=False,
            error=result.error,
        )

    # classify_pdf_pages stores 1-indexed page numbers.
    # extract_all_schedule_pages expects 0-indexed.
    zero_indexed = [p - 1 for p in result.schedule_pages]

    return RouterClassifierResult(
        success=True,
        total_pages=result.total_pages,
        schedule_pages=zero_indexed,
        has_schedules=len(zero_indexed) > 0,
        error=result.error,
    )