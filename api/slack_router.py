# api/slack_router.py
# Slack interactive actions webhook endpoint
# Handles Approve / Reject / Hold button clicks from approval messages

import json
import logging
import os
from urllib.parse import parse_qs
from fastapi import APIRouter, Request, HTTPException
from integrations.slack_client import (
    update_approval_message,
    post_notification,
    verify_slack_signature,
)
from integrations.airtable_client import get_airtable_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["Slack Webhooks"])


# ---------------------------------------------------------------------------
# POST /webhooks/slack/actions
# Receives interactive button clicks from Slack
# ---------------------------------------------------------------------------

@router.post("/slack/actions", summary="Handle Slack interactive button actions")
async def slack_actions(request: Request):
    """
    Receives Slack interactive component payloads (button clicks).
    Verifies signature, extracts decision, updates bill in Airtable,
    and updates the Slack message to reflect the decision.
    """
    body_bytes = await request.body()

    # Verify Slack signature
    signing_secret = os.getenv("SLACK_SIGNING_SECRET", "")
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if signing_secret and not verify_slack_signature(
        signing_secret, body_bytes, timestamp, signature
    ):
        raise HTTPException(status_code=403, detail="Invalid Slack signature.")

    # Parse payload (Slack sends as URL-encoded form with 'payload' key)
    body_str = body_bytes.decode("utf-8")
    parsed = parse_qs(body_str)
    payload_str = parsed.get("payload", ["{}"])[0]

    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid payload JSON.")

    payload_type = payload.get("type")

    # Handle block_actions (button clicks)
    if payload_type == "block_actions":
        return await _handle_block_action(payload)

    # URL verification challenge from Slack
    if payload_type == "url_verification":
        return {"challenge": payload.get("challenge")}

    logger.info(f"Unhandled Slack payload type: {payload_type}")
    return {"ok": True}


async def _handle_block_action(payload: dict) -> dict:
    """Process a button click action from an approval message."""
    actions = payload.get("actions", [])
    if not actions:
        return {"ok": True}

    action = actions[0]
    action_id = action.get("action_id")
    value_str = action.get("value", "{}")

    try:
        value = json.loads(value_str)
    except json.JSONDecodeError:
        logger.error(f"Invalid action value: {value_str}")
        return {"ok": True}

    bill_id = value.get("bill_id")
    decision = value.get("decision")

    # Get who clicked
    user = payload.get("user", {})
    decided_by = user.get("name") or user.get("id", "Unknown")

    # Get message metadata for updating
    message = payload.get("message", {})
    message_ts = message.get("ts", "")
    channel = payload.get("channel", {}).get("id", "")

    logger.info(
        f"Slack action received | "
        f"action={action_id} | bill_id={bill_id} | "
        f"decision={decision} | user={decided_by}"
    )

    if not bill_id or not decision:
        logger.error("Missing bill_id or decision in Slack action payload.")
        return {"ok": True}

    # Update Airtable bill status
    airtable = get_airtable_client()
    bill = airtable.get_bill_by_id(bill_id)

    if not bill:
        logger.error(f"Bill {bill_id} not found in Airtable.")
        post_notification(f"⚠️ Error: Bill `{bill_id}` not found when processing {decision} decision.")
        return {"ok": True}

    invoice_number = bill.get("Invoice_Number", bill_id)
    vendor_name = bill.get("Vendor_Name", "Unknown Vendor")
    amount = float(bill.get("Total_Amount", 0))

    # Map decision to Airtable status
    status_map = {
        "approve": "Approved",
        "reject": "Rejected",
        "hold": "On Hold",
    }
    new_status = status_map.get(decision, "On Hold")

    # Update bill in Airtable
    airtable.update_bill(bill_id, {
        "Approval_Status": new_status,
        "Approved_By": decided_by,
        "Approved_At": __import__("datetime").datetime.utcnow().isoformat(),
    })

    # Update Slack message to show decision
    update_approval_message(
        channel=channel,
        message_ts=message_ts,
        bill_id=bill_id,
        invoice_number=invoice_number,
        vendor_name=vendor_name,
        amount=amount,
        decision=decision,
        decided_by=decided_by,
    )

    # Post follow-up note for rejected/held bills
    if decision == "reject":
        post_notification(
            f"❌ Invoice *{invoice_number}* from *{vendor_name}* "
            f"(${amount:,.2f}) was *REJECTED* by {decided_by}. "
            f"Bill ID: `{bill_id}`"
        )
    elif decision == "hold":
        post_notification(
            f"⏸ Invoice *{invoice_number}* from *{vendor_name}* "
            f"(${amount:,.2f}) placed *ON HOLD* by {decided_by}. "
            f"Bill ID: `{bill_id}`"
        )

    logger.info(
        f"Bill {bill_id} updated to {new_status} by {decided_by}"
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# GET /webhooks/slack/health
# ---------------------------------------------------------------------------

@router.get("/slack/health", summary="Slack integration health check")
async def slack_health():
    """Verify Slack credentials are configured."""
    token = os.getenv("SLACK_BOT_TOKEN", "")
    channel = os.getenv("SLACK_APPROVAL_CHANNEL", "")
    return {
        "status": "ok" if token and channel else "misconfigured",
        "token_set": bool(token),
        "channel_set": bool(channel),
        "channel_id": channel,
    }
