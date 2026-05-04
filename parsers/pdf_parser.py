# parsers/pdf_parser.py

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
import pytesseract
from PIL import Image

from core.logger import get_logger

logger = get_logger(__name__)

# Windows path to Tesseract binary — update if your install path differs
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

if os.path.exists(TESSERACT_CMD):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


class PDFParseResult:
    """Holds the result of a PDF text extraction attempt."""

    def __init__(
        self,
        text: str,
        page_count: int,
        extraction_method: str,
        file_path: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self.text = text
        self.page_count = page_count
        self.extraction_method = extraction_method
        self.file_path = file_path
        self.error = error
        self.success = error is None and len(text.strip()) > 0

    def __repr__(self) -> str:
        return (
            f"PDFParseResult(method={self.extraction_method}, "
            f"pages={self.page_count}, "
            f"chars={len(self.text)}, "
            f"success={self.success})"
        )


def _extract_with_pymupdf(pdf_bytes: bytes) -> tuple[str, int]:
    """
    Extract text directly from a digital PDF using PyMuPDF.
    Returns (full_text, page_count).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_count = len(doc)
    pages_text: list[str] = []

    for page_num in range(page_count):
        page = doc[page_num]
        text = page.get_text("text")
        pages_text.append(text)

    doc.close()
    full_text = "\n".join(pages_text)
    return full_text, page_count


def _extract_with_ocr(pdf_bytes: bytes) -> tuple[str, int]:
    """
    Convert each PDF page to an image and run Tesseract OCR.
    Used as fallback for scanned or image-based PDFs.
    Returns (full_text, page_count).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_count = len(doc)
    pages_text: list[str] = []

    for page_num in range(page_count):
        page = doc[page_num]

        # Render page to image at 300 DPI for accurate OCR
        mat = fitz.Matrix(300 / 72, 300 / 72)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")

        image = Image.open(io.BytesIO(img_bytes))
        text = pytesseract.image_to_string(image, lang="eng")
        pages_text.append(text)

    doc.close()
    full_text = "\n".join(pages_text)
    return full_text, page_count


def _is_text_sufficient(text: str, min_chars: int = 50) -> bool:
    """
    Check whether PyMuPDF extracted meaningful text.
    Scanned PDFs return empty or near-empty strings.
    """
    cleaned = text.strip().replace("\n", "").replace(" ", "")
    return len(cleaned) >= min_chars


def parse_pdf(source: str | bytes | Path) -> PDFParseResult:
    """
    Main entry point for PDF parsing.

    Accepts:
        - A file path (str or Path)
        - Raw PDF bytes

    Strategy:
        1. Try PyMuPDF text extraction.
        2. If extracted text is insufficient, fall back to OCR.
        3. Return a PDFParseResult with text, method, and metadata.
    """
    # Normalize input to bytes
    if isinstance(source, (str, Path)):
        file_path = str(source)
        try:
            with open(file_path, "rb") as f:
                pdf_bytes = f.read()
        except FileNotFoundError as e:
            logger.error("PDF file not found", path=file_path, error=str(e))
            return PDFParseResult(
                text="",
                page_count=0,
                extraction_method="none",
                file_path=file_path,
                error=f"File not found: {file_path}",
            )
    else:
        pdf_bytes = source
        file_path = None

    # Attempt 1: PyMuPDF direct extraction
    try:
        text, page_count = _extract_with_pymupdf(pdf_bytes)

        if _is_text_sufficient(text):
            logger.info(
                "PDF extracted via PyMuPDF",
                pages=page_count,
                chars=len(text),
                file=file_path,
            )
            return PDFParseResult(
                text=text,
                page_count=page_count,
                extraction_method="pymupdf",
                file_path=file_path,
            )

        logger.info(
            "PyMuPDF returned insufficient text — falling back to OCR",
            chars=len(text),
            file=file_path,
        )

    except Exception as e:
        logger.warning(
            "PyMuPDF extraction failed — falling back to OCR",
            error=str(e),
            file=file_path,
        )
        page_count = 0

    # Attempt 2: OCR fallback
    try:
        text, page_count = _extract_with_ocr(pdf_bytes)

        if _is_text_sufficient(text):
            logger.info(
                "PDF extracted via OCR",
                pages=page_count,
                chars=len(text),
                file=file_path,
            )
            return PDFParseResult(
                text=text,
                page_count=page_count,
                extraction_method="ocr",
                file_path=file_path,
            )

        logger.warning(
            "OCR also returned insufficient text",
            chars=len(text),
            file=file_path,
        )
        return PDFParseResult(
            text=text,
            page_count=page_count,
            extraction_method="ocr",
            file_path=file_path,
            error="Extracted text too short — document may be blank or corrupt",
        )

    except Exception as e:
        logger.error(
            "OCR extraction failed",
            error=str(e),
            file=file_path,
        )
        return PDFParseResult(
            text="",
            page_count=0,
            extraction_method="ocr_failed",
            file_path=file_path,
            error=f"OCR failed: {str(e)}",
        )