# api/approval_router.py

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel

from core.database import get_supabase, update_approval, update_invoice_status
from core.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/approval", tags=["Approval"])


# ------------------------------------------------------------------
# Request model
# ------------------------------------------------------------------

class ApprovalDecisionRequest(BaseModel):
    token: str
    decision: str  # "approve" or "reject"
    rejection_reason: str = ""


# ------------------------------------------------------------------
# One-click approve/reject via token (email link)
# ------------------------------------------------------------------

@router.get("/decide")
async def decide_via_link(token: str, decision: str):
    """
    Called when an approver clicks the approve or reject link in their email.
    Example: /approval/decide?token=abc123&decision=approve
    """
    log = logger.bind(endpoint="decide_via_link", token=token, decision=decision)

    if decision not in ("approve", "reject"):
        raise HTTPException(
            status_code=400,
            detail="Decision must be 'approve' or 'reject'.",
        )

    try:
        client = get_supabase()

        # Look up approval record by token
        result = (
            client.table("ap_approval_records")
            .select("*")
            .eq("token", token)
            .execute()
        )

        if not result.data:
            raise HTTPException(
                status_code=404,
                detail="Approval token not found or already used.",
            )

        record = result.data[0]

        if record["status"] != "pending":
            return HTMLResponse(
                content=f"""
                <html><body>
                <h2>Already Processed</h2>
                <p>Invoice <b>{record['invoice_number']}</b> has already been {record['status']}.</p>
                </body></html>
                """,
                status_code=200,
            )

        # Update approval record
        from datetime import datetime, timezone
        update_approval(
            approval_id=record["approval_id"],
            data={
                "status": decision + "d",  # approved / rejected
                "decision": decision,
                "decided_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        # Update invoice status
        new_status = "approved" if decision == "approve" else "exception"
        update_invoice_status(record["invoice_number"], new_status)

        log.info(
            "approval_decision_recorded",
            invoice_number=record["invoice_number"],
            decision=decision,
        )

        return HTMLResponse(
            content=f"""
            <html><body>
            <h2>Decision Recorded</h2>
            <p>Invoice <b>{record['invoice_number']}</b> has been <b>{decision}d</b>.</p>
            <p>You may close this window.</p>
            </body></html>
            """,
            status_code=200,
        )

    except HTTPException:
        raise
    except Exception as exc:
        log.error("approval_decision_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Approval decision failed: {str(exc)}")


# ------------------------------------------------------------------
# Programmatic approve/reject via JSON body
# ------------------------------------------------------------------

@router.post("/decide")
async def decide_via_api(payload: ApprovalDecisionRequest):
    """
    Programmatic approval decision via API call with JSON body.
    Used by internal systems or admin tools.
    """
    log = logger.bind(endpoint="decide_via_api", token=payload.token)

    if payload.decision not in ("approve", "reject"):
        raise HTTPException(
            status_code=400,
            detail="Decision must be 'approve' or 'reject'.",
        )

    try:
        client = get_supabase()

        result = (
            client.table("ap_approval_records")
            .select("*")
            .eq("token", payload.token)
            .execute()
        )

        if not result.data:
            raise HTTPException(
                status_code=404,
                detail="Approval token not found.",
            )

        record = result.data[0]

        if record["status"] != "pending":
            return JSONResponse(
                status_code=200,
                content={
                    "status": "already_processed",
                    "invoice_number": record["invoice_number"],
                    "current_status": record["status"],
                },
            )

        from datetime import datetime, timezone
        update_approval(
            approval_id=record["approval_id"],
            data={
                "status": payload.decision + "d",
                "decision": payload.decision,
                "rejection_reason": payload.rejection_reason,
                "decided_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        new_status = "approved" if payload.decision == "approve" else "exception"
        update_invoice_status(record["invoice_number"], new_status)

        log.info(
            "api_approval_decision_recorded",
            invoice_number=record["invoice_number"],
            decision=payload.decision,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "invoice_number": record["invoice_number"],
                "decision": payload.decision,
            },
        )

    except HTTPException:
        raise
    except Exception as exc:
        log.error("api_approval_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Approval failed: {str(exc)}")


# ------------------------------------------------------------------
# Status query endpoint
# ------------------------------------------------------------------

@router.get("/status/{invoice_number}")
async def get_approval_status(invoice_number: str):
    """Query the current approval status of an invoice."""
    log = logger.bind(endpoint="get_approval_status", invoice_number=invoice_number)

    try:
        client = get_supabase()

        result = (
            client.table("ap_approval_records")
            .select("*")
            .eq("invoice_number", invoice_number)
            .order("requested_at", desc=True)
            .limit(1)
            .execute()
        )

        if not result.data:
            raise HTTPException(
                status_code=404,
                detail=f"No approval record found for invoice {invoice_number}.",
            )

        record = result.data[0]
        log.info("approval_status_queried", invoice_number=invoice_number)

        return JSONResponse(
            status_code=200,
            content={
                "invoice_number": invoice_number,
                "approval_id": record["approval_id"],
                "approver_email": record["approver_email"],
                "status": record["status"],
                "decision": record.get("decision"),
                "rejection_reason": record.get("rejection_reason"),
                "requested_at": record["requested_at"],
                "decided_at": record.get("decided_at"),
            },
        )

    except HTTPException:
        raise
    except Exception as exc:
        log.error("approval_status_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Status query failed: {str(exc)}")