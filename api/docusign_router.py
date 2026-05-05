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
# Internal handlers - wired to compliance agent
# ---------------------------------------------------------------------------

async def _handle_completed(envelope_id: str, payload: dict):
    """
    Envelope completed: all parties signed.
    Downloads documents, runs name consistency check, updates Airtable.
    """
    from agents.compliance_agent import get_compliance_agent
    from integrations.slack_client import post_notification
    from integrations.airtable_client import get_airtable_client

    logger.info(f"Envelope COMPLETED: {envelope_id}. Starting document processing.")

    try:
        agent = get_compliance_agent()
        result = agent.process_completed_envelope(envelope_id)

        if result.get("success"):
            # Find bill linked to this envelope and re-run compliance check
            airtable = get_airtable_client()
            formula = f"{{DocuSign_Envelope_ID}}='{envelope_id}'"
            bills = airtable._table("Bills").all(formula=formula)

            for bill_record in bills:
                bill_id = bill_record["id"]
                compliance_result = agent.check_bill_compliance(bill_id)

                if compliance_result.get("can_proceed"):
                    post_notification(
                        f"✅ Compliance documents received and verified for envelope "
                        f"`{envelope_id}`. "
                        f"Bill `{bill_id}` is now ready for cost coding and approval."
                    )
                    logger.info(f"Bill {bill_id} now compliant after envelope {envelope_id} completed.")
                else:
                    post_notification(
                        f"⚠️ Envelope `{envelope_id}` completed but compliance check failed: "
                        f"{compliance_result.get('note')}"
                    )
        else:
            logger.error(f"Document download failed for envelope {envelope_id}: {result.get('error')}")
            post_notification(
                f"❌ Failed to download documents for completed envelope `{envelope_id}`. "
                f"Error: {result.get('error', 'Unknown')}. Manual review required."
            )

    except Exception as e:
        logger.error(f"Error processing completed envelope {envelope_id}: {e}")


async def _handle_declined(envelope_id: str, payload: dict):
    """
    Envelope declined by vendor.
    Notifies operator and flags if Master Policy was refused.
    """
    from agents.compliance_agent import get_compliance_agent
    from integrations.slack_client import post_notification
    from integrations.airtable_client import get_airtable_client

    logger.warning(f"Envelope DECLINED: {envelope_id}.")

    try:
        agent = get_compliance_agent()
        agent.handle_declined_envelope(envelope_id)

        # Find linked bills and update status
        airtable = get_airtable_client()
        formula = f"{{DocuSign_Envelope_ID}}='{envelope_id}'"
        bills = airtable._table("Bills").all(formula=formula)

        bill_ids = [b["id"] for b in bills]
        for bill_id in bill_ids:
            airtable.update_bill_compliance_status(
                bill_id,
                "Blocked",
                f"Vendor declined DocuSign envelope {envelope_id}. Manual follow-up required."
            )

        post_notification(
            f"🚫 Vendor declined compliance envelope `{envelope_id}`. "
            f"Affected bills: {', '.join(f'`{b}`' for b in bill_ids) or 'none found'}. "
            f"If vendor refused Master Insurance Policy, mark as Refused Compliance in Airtable."
        )

    except Exception as e:
        logger.error(f"Error handling declined envelope {envelope_id}: {e}")


async def _handle_voided(envelope_id: str, payload: dict):
    """
    Envelope voided or expired.
    Updates bill status and notifies operator to re-send.
    """
    from agents.compliance_agent import get_compliance_agent
    from integrations.slack_client import post_notification
    from integrations.airtable_client import get_airtable_client

    logger.warning(f"Envelope VOIDED: {envelope_id}.")

    try:
        agent = get_compliance_agent()
        agent.handle_voided_envelope(envelope_id)

        airtable = get_airtable_client()
        formula = f"{{DocuSign_Envelope_ID}}='{envelope_id}'"
        bills = airtable._table("Bills").all(formula=formula)

        for bill_record in bills:
            airtable.update_bill_compliance_status(
                bill_record["id"],
                "Awaiting Docs",
                f"Envelope {envelope_id} expired or voided after 30 days. New envelope required."
            )

        post_notification(
            f"⏰ Compliance envelope `{envelope_id}` has expired or been voided. "
            f"A new envelope must be sent to the vendor before this bill can proceed."
        )

    except Exception as e:
        logger.error(f"Error handling voided envelope {envelope_id}: {e}")


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
