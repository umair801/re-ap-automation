# api/metrics_router.py

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from datetime import datetime, timezone

from core.database import get_supabase, get_metrics

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/metrics", tags=["Metrics"])


@router.get("")
async def get_ap_metrics():
    """
    Returns real-time AP processing KPIs from Supabase.
    Use this endpoint to demonstrate system performance to clients.
    """
    log = logger.bind(endpoint="get_ap_metrics")

    try:
        base_metrics = get_metrics()

        client = get_supabase()

        # Exception rate
        total = base_metrics["invoices_processed"]
        exceptions = base_metrics["open_exceptions"]
        matched = base_metrics["invoices_matched"]
        paid = base_metrics["invoices_paid"]

        exception_rate = round((exceptions / total * 100), 2) if total > 0 else 0.0
        match_rate = round((matched / total * 100), 2) if total > 0 else 0.0
        payment_rate = round((paid / total * 100), 2) if total > 0 else 0.0

        # Pending approvals count
        pending_approvals = (
            client.table("ap_approval_records")
            .select("id", count="exact")
            .eq("status", "pending")
            .execute()
        )

        # Invoices by status breakdown
        status_breakdown = {}
        for status in [
            "received", "extracted", "validated", "matched",
            "pending_approval", "approved", "exception",
            "payment_scheduled", "paid"
        ]:
            result = (
                client.table("ap_invoices")
                .select("id", count="exact")
                .eq("status", status)
                .execute()
            )
            status_breakdown[status] = result.count or 0

        # Invoices by source
        source_breakdown = {}
        for source in ["pdf_upload", "email_webhook", "edi"]:
            result = (
                client.table("ap_invoices")
                .select("id", count="exact")
                .eq("source", source)
                .execute()
            )
            source_breakdown[source] = result.count or 0

        log.info("metrics_fetched", total_invoices=total)

        return JSONResponse(
            status_code=200,
            content={
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "summary": {
                    "invoices_processed": total,
                    "invoices_matched": matched,
                    "invoices_paid": paid,
                    "open_exceptions": exceptions,
                    "pending_approvals": pending_approvals.count or 0,
                },
                "rates": {
                    "match_rate_pct": match_rate,
                    "exception_rate_pct": exception_rate,
                    "payment_completion_rate_pct": payment_rate,
                },
                "status_breakdown": status_breakdown,
                "source_breakdown": source_breakdown,
                "targets": {
                    "match_rate_target_pct": 98.0,
                    "exception_rate_target_pct": 5.0,
                    "payment_completion_target_pct": 95.0,
                },
            },
        )

    except Exception as exc:
        log.error("metrics_fetch_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Metrics fetch failed: {str(exc)}")


@router.get("/health")
async def health_check():
    """Simple health check endpoint for Railway and uptime monitoring."""
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "AP-AI Accounts Payable Automation Agent",
            "brand": "Datawebify",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )