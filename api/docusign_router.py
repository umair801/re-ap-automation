# api/docusign_router.py
# FastAPI webhook endpoint for DocuSign Connect events

import json
import logging
from fastapi import APIRouter, Request, HTTPException, Header
from typing import Optional

from integrations.docusign_client import get_docusign_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["DocuSign Webhooks"])


# ---------------------------------------------------------------------------
# POST /webhooks/docusign
# ---------------------------------------------------------------------------

@router.post("/docusign", summary="Receive DocuSign Connect envelope events")
async def docusign_webhook(
    request: Request,
    x_docusign_signature_1: Optional[str] = Header(None),
):
    """
    Receives DocuSign Connect webhook events when an envelope status changes.

    DocuSign sends this when:
    - Envelope is completed (all parties signed)
    - Envelope is declined (vendor refused)
    - Envelope is voided
    - Envelope expires after 30 days

    Security: HMAC-SHA256 signature verified against DOCUSIGN_CONNECT_SECRET.
    If the secret is not yet configured, signature check is logged but not enforced
    (sandbox only). In production, unverified requests are rejected with 403.
    """
    payload_bytes = await request.body()

    # --- Signature verification ---
    client = get_docusign_client()

    if x_docusign_signature_1:
        is_valid = client.verify_connect_signature(
            payload_bytes=payload_bytes,
            signature_header=x_docusign_signature_1,
        )
        if not is_valid:
            if client.sandbox:
                logger.warning(
                    "DocuSign signature verification failed — sandbox mode, continuing."
                )
            else:
                raise HTTPException(status_code=403, detail="Invalid DocuSign signature.")
    else:
        if not client.sandbox:
            raise HTTPException(
                status_code=403,
                detail="Missing X-DocuSign-Signature-1 header.",
            )
        logger.warning("No DocuSign signature header present — sandbox mode, continuing.")

    # --- Parse event ---
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        logger.error("DocuSign webhook: failed to parse JSON payload.")
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    envelope_id = payload.get("envelopeId") or payload.get("data", {}).get("envelopeId", "unknown")
    status = payload.get("status") or payload.get("data", {}).get("envelopeSummary", {}).get("status", "unknown")
    event = payload.get("event", "unknown")

    logger.info(
        f"DocuSign Connect event received | "
        f"event={event} | envelope_id={envelope_id} | status={status}"
    )

    # --- Route by status ---
    if status == "completed":
        await _handle_completed(envelope_id, payload)
    elif status == "declined":
        await _handle_declined(envelope_id, payload)
    elif status == "voided":
        await _handle_voided(envelope_id, payload)
    elif status == "sent" or status == "delivered":
        logger.info(f"Envelope {envelope_id} is in transit (status={status}). No action needed.")
    else:
        logger.info(f"Envelope {envelope_id} unhandled status: {status}. Logged only.")

    return {"received": True, "envelope_id": envelope_id, "status": status}


# ---------------------------------------------------------------------------
# Internal handlers (stubs — wired to compliance agent in GAP 3)
# ---------------------------------------------------------------------------

async def _handle_completed(envelope_id: str, payload: dict):
    """
    Envelope completed: all parties have signed.
    GAP 3 (compliance_agent) will call document download and verification here.
    """
    logger.info(
        f"Envelope COMPLETED: {envelope_id}. "
        f"Queuing compliance document download and verification."
    )
    # TODO (GAP 3): call compliance_agent.process_completed_envelope(envelope_id)


async def _handle_declined(envelope_id: str, payload: dict):
    """
    Envelope declined by vendor.
    If vendor declined the Master Insurance Policy, flag as Refused Compliance in Airtable.
    """
    logger.warning(
        f"Envelope DECLINED: {envelope_id}. "
        f"Operator must review and flag vendor if Master Policy was refused."
    )
    # TODO (GAP 3): call compliance_agent.handle_declined_envelope(envelope_id)


async def _handle_voided(envelope_id: str, payload: dict):
    """
    Envelope voided (expired or manually voided).
    """
    logger.warning(
        f"Envelope VOIDED: {envelope_id}. "
        f"Vendor compliance paused. Bill remains blocked until new envelope is sent."
    )
    # TODO (GAP 3): call compliance_agent.handle_voided_envelope(envelope_id)


# ---------------------------------------------------------------------------
# GET /webhooks/docusign/health
# ---------------------------------------------------------------------------

@router.get("/docusign/health", summary="DocuSign integration health check")
async def docusign_health():
    """
    Verifies DocuSign credentials are configured and JWT auth succeeds.
    Use this to confirm the integration is live before going to production.
    """
    client = get_docusign_client()
    try:
        client._authenticate()
        return {
            "status": "ok",
            "sandbox": client.sandbox,
            "account_id": client.account_id[:8] + "...",
            "auth": "JWT Grant SUCCESS",
        }
    except Exception as e:
        return {
            "status": "error",
            "detail": str(e),
        }
