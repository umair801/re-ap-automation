# api/ingestion_router.py

from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, File, HTTPException, UploadFile, Request
from fastapi.responses import JSONResponse

from core.database import insert_invoice, get_existing_invoice_numbers
from core.config import get_settings
from agents.extraction_agent import run_extraction_agent
from agents.validation_agent import run_validation_agent
from core.models import Invoice, InvoiceStatus, IngestionSource

logger = structlog.get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/ingest", tags=["Ingestion"])


# ------------------------------------------------------------------
# PDF Upload Endpoint
# ------------------------------------------------------------------

@router.post("/pdf")
async def ingest_pdf(file: Annotated[UploadFile, File(description="Invoice PDF file")]):
    """Accept a PDF invoice upload and run it through the extraction pipeline."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    log = logger.bind(endpoint="ingest_pdf", filename=file.filename)
    log.info("pdf_upload_received")

    try:
        contents = await file.read()
        if len(contents) == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")

        invoice = Invoice(source=IngestionSource.PDF_UPLOAD)
        invoice = run_extraction_agent(invoice, contents)
        existing_numbers = get_existing_invoice_numbers()
        invoice = run_validation_agent(invoice, existing_invoice_numbers=existing_numbers)

        is_valid = invoice.status == InvoiceStatus.VALIDATED
        vendor_name = invoice.vendor.vendor_name if invoice.vendor else None
        error_codes = [e.error_code for e in invoice.validation_errors]
        is_duplicate = "DUPLICATE_INVOICE" in error_codes

        if not is_duplicate:
            invoice_data = {
                "invoice_number": invoice.invoice_number,
                "vendor_name": vendor_name,
                "invoice_date": str(invoice.invoice_date) if invoice.invoice_date else None,
                "due_date": str(invoice.due_date) if invoice.due_date else None,
                "total_amount": float(invoice.total) if invoice.total else None,
                "subtotal": float(invoice.subtotal) if invoice.subtotal else None,
                "tax_amount": float(invoice.tax) if invoice.tax else None,
                "currency": invoice.currency,
                "po_number": invoice.po_number,
                "payment_terms": invoice.payment_terms,
                "status": invoice.status,
                "source": IngestionSource.PDF_UPLOAD.value,
            }
            insert_invoice(invoice_data)

        log.info(
            "pdf_invoice_processed",
            invoice_number=invoice.invoice_number,
            valid=is_valid,
            duplicate=is_duplicate,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "duplicate" if is_duplicate else "success",
                "invoice_number": invoice.invoice_number,
                "vendor_name": vendor_name,
                "total_amount": float(invoice.total) if invoice.total else None,
                "validation_passed": is_valid,
                "validation_errors": error_codes,
            },
        )

    except HTTPException:
        raise
    except Exception as exc:
        log.error("pdf_ingest_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(exc)}")


# ------------------------------------------------------------------
# Email Webhook Endpoint
# ------------------------------------------------------------------

@router.post("/email-webhook")
async def ingest_email_webhook(request: Request):
    """Receive an email webhook payload from Gmail or Outlook."""
    log = logger.bind(endpoint="ingest_email_webhook")

    try:
        payload = await request.json()
        log.info("email_webhook_received", keys=list(payload.keys()))

        sender = payload.get("from", "unknown")
        body = payload.get("body", "")
        attachments = payload.get("attachments", [])

        if not body and not attachments:
            return JSONResponse(
                status_code=200,
                content={"status": "ignored", "reason": "No body or attachments found."},
            )

        from agents.extraction_agent import extract_invoice_text_mode
        invoice = Invoice(source=IngestionSource.EMAIL)
        invoice = extract_invoice_text_mode(invoice, body)
        existing_numbers = get_existing_invoice_numbers()
        invoice = run_validation_agent(invoice, existing_invoice_numbers=existing_numbers)

        email_is_valid = invoice.status == InvoiceStatus.VALIDATED
        email_vendor_name = invoice.vendor.vendor_name if invoice.vendor else None
        email_error_codes = [e.error_code for e in invoice.validation_errors]
        email_is_duplicate = "DUPLICATE_INVOICE" in email_error_codes

        if not email_is_duplicate:
            invoice_data = {
                "invoice_number": invoice.invoice_number,
                "vendor_name": email_vendor_name,
                "invoice_date": str(invoice.invoice_date) if invoice.invoice_date else None,
                "due_date": str(invoice.due_date) if invoice.due_date else None,
                "total_amount": float(invoice.total) if invoice.total else None,
                "currency": invoice.currency,
                "po_number": invoice.po_number,
                "status": invoice.status,
                "source": IngestionSource.EMAIL.value,
            }
            insert_invoice(invoice_data)

        log.info(
            "email_invoice_processed",
            invoice_number=invoice.invoice_number,
            sender=sender,
            valid=email_is_valid,
            duplicate=email_is_duplicate,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "duplicate" if email_is_duplicate else "success",
                "invoice_number": invoice.invoice_number,
                "vendor_name": email_vendor_name,
                "validation_passed": email_is_valid,
                "validation_errors": email_error_codes,
            },
        )

    except Exception as exc:
        log.error("email_webhook_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Email webhook failed: {str(exc)}")


# ------------------------------------------------------------------
# EDI File Upload Endpoint
# ------------------------------------------------------------------

@router.post("/edi")
async def ingest_edi(file: Annotated[UploadFile, File(description="EDI 810 invoice file")]):
    """Accept an EDI 810 invoice file and parse it into the pipeline."""
    log = logger.bind(endpoint="ingest_edi", filename=file.filename)
    log.info("edi_upload_received")

    try:
        contents = await file.read()
        if len(contents) == 0:
            raise HTTPException(status_code=400, detail="Uploaded EDI file is empty.")

        edi_text = contents.decode("utf-8", errors="replace")

        if "ST*810" not in edi_text and "810" not in edi_text[:100]:
            raise HTTPException(
                status_code=400,
                detail="File does not appear to be a valid EDI 810 invoice.",
            )

        invoice = Invoice(source=IngestionSource.EDI)
        invoice = run_extraction_agent(invoice, contents)
        existing_numbers = get_existing_invoice_numbers()
        invoice = run_validation_agent(invoice, existing_invoice_numbers=existing_numbers)

        edi_is_valid = invoice.status == InvoiceStatus.VALIDATED
        edi_vendor_name = invoice.vendor.vendor_name if invoice.vendor else None
        edi_error_codes = [e.error_code for e in invoice.validation_errors]
        edi_is_duplicate = "DUPLICATE_INVOICE" in edi_error_codes

        if not edi_is_duplicate:
            invoice_data = {
                "invoice_number": invoice.invoice_number,
                "vendor_name": edi_vendor_name,
                "invoice_date": str(invoice.invoice_date) if invoice.invoice_date else None,
                "due_date": str(invoice.due_date) if invoice.due_date else None,
                "total_amount": float(invoice.total) if invoice.total else None,
                "currency": invoice.currency,
                "po_number": invoice.po_number,
                "status": invoice.status,
                "source": IngestionSource.EDI.value,
            }
            insert_invoice(invoice_data)

        log.info(
            "edi_invoice_processed",
            invoice_number=invoice.invoice_number,
            valid=edi_is_valid,
            duplicate=edi_is_duplicate,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "duplicate" if edi_is_duplicate else "success",
                "invoice_number": invoice.invoice_number,
                "vendor_name": edi_vendor_name,
                "validation_passed": edi_is_valid,
                "validation_errors": edi_error_codes,
            },
        )

    except HTTPException:
        raise
    except Exception as exc:
        log.error("edi_ingest_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"EDI ingestion failed: {str(exc)}")