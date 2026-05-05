# integrations/slack_client.py
# Slack integration for AP Automation System
# Handles approval notifications with interactive buttons (Approve/Reject/Hold)
# 48-hour reminders and 96-hour escalation to Principal

import os
import json
import logging
import hashlib
import hmac
from datetime import datetime
from typing import Optional
import httpx
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"


# ---------------------------------------------------------------------------
# Approval tier config matching the spec
# ---------------------------------------------------------------------------

APPROVAL_TIERS = {
    "tier_a": {"label": "Tier A — AP Manager",       "limit": 25000},
    "tier_b": {"label": "Tier B — Finance Director", "limit": 50000},
    "tier_c": {"label": "Tier C — Principal",        "limit": 75000},
    "tier_d": {"label": "Tier D — Principal + Escalation", "limit": None},
}


def _get_tier(amount: float) -> str:
    if amount <= 25000:
        return "tier_a"
    elif amount <= 50000:
        return "tier_b"
    elif amount <= 75000:
        return "tier_c"
    else:
        return "tier_d"


# ---------------------------------------------------------------------------
# Core Slack API caller
# ---------------------------------------------------------------------------

def _slack_post(method: str, payload: dict) -> dict:
    """POST to Slack API. Returns response JSON."""
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        logger.warning("SLACK_BOT_TOKEN not configured.")
        return {"ok": False, "error": "token_missing"}

    try:
        response = httpx.post(
            f"{SLACK_API_BASE}/{method}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10,
        )
        result = response.json()
        if not result.get("ok"):
            logger.error(f"Slack API error: {result.get('error')} | method={method}")
        return result
    except Exception as e:
        logger.error(f"Slack API call failed: {e}")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Build approval message blocks
# ---------------------------------------------------------------------------

def _build_approval_blocks(
    bill_id: str,
    invoice_number: str,
    vendor_name: str,
    amount: float,
    entity_name: str,
    project_name: str,
    gl_lines: list[dict],
    compliance_summary: str,
    tier_label: str,
    is_reminder: bool = False,
    is_escalation: bool = False,
) -> list[dict]:
    """Build Slack Block Kit message for approval request."""

    header_text = "🔔 Invoice Approval Required"
    if is_escalation:
        header_text = "🚨 ESCALATION: Invoice Approval Required (96hr)"
    elif is_reminder:
        header_text = "⏰ Reminder: Invoice Approval Still Pending (48hr)"

    # Format GL lines for display
    gl_text = "\n".join(
        f"• {line.get('description', 'Line item')}: "
        f"${line.get('amount', 0):,.2f} "
        f"[{line.get('cs_code', '')}] → GL {line.get('gl_account', '')}"
        for line in gl_lines[:5]  # Show max 5 lines
    ) or "No line items"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True}
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Invoice:*\n{invoice_number}"},
                {"type": "mrkdwn", "text": f"*Vendor:*\n{vendor_name}"},
                {"type": "mrkdwn", "text": f"*Amount:*\n${amount:,.2f}"},
                {"type": "mrkdwn", "text": f"*Entity:*\n{entity_name}"},
                {"type": "mrkdwn", "text": f"*Project:*\n{project_name}"},
                {"type": "mrkdwn", "text": f"*Approval Tier:*\n{tier_label}"},
            ]
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*GL Coding:*\n{gl_text}"}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Compliance:*\n{compliance_summary}"}
        },
        {"type": "divider"},
        {
            "type": "actions",
            "block_id": f"approval_actions_{bill_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
                    "style": "primary",
                    "action_id": "approve_bill",
                    "value": json.dumps({"bill_id": bill_id, "decision": "approve"}),
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Confirm Approval"},
                        "text": {"type": "mrkdwn", "text": f"Approve *{invoice_number}* for *${amount:,.2f}*?"},
                        "confirm": {"type": "plain_text", "text": "Yes, Approve"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    }
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
                    "style": "danger",
                    "action_id": "reject_bill",
                    "value": json.dumps({"bill_id": bill_id, "decision": "reject"}),
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Confirm Rejection"},
                        "text": {"type": "mrkdwn", "text": f"Reject *{invoice_number}*?"},
                        "confirm": {"type": "plain_text", "text": "Yes, Reject"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    }
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏸ Hold", "emoji": True},
                    "action_id": "hold_bill",
                    "value": json.dumps({"bill_id": bill_id, "decision": "hold"}),
                },
            ]
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Bill ID: `{bill_id}` | "
                        f"Sent: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} | "
                        f"Reminder at 48hr | Escalation at 96hr"
                    )
                }
            ]
        }
    ]
    return blocks


# ---------------------------------------------------------------------------
# Send approval message
# ---------------------------------------------------------------------------

def send_approval_message(
    bill_id: str,
    invoice_number: str,
    vendor_name: str,
    amount: float,
    entity_name: str,
    project_name: str,
    gl_lines: list[dict],
    compliance_summary: str,
    is_reminder: bool = False,
    is_escalation: bool = False,
    thread_ts: Optional[str] = None,
) -> dict:
    """
    Send an approval request to the Slack approval channel.

    Args:
        bill_id:             Airtable bill record ID.
        invoice_number:      Invoice number for display.
        vendor_name:         Vendor legal name.
        amount:              Total invoice amount.
        entity_name:         LLC entity name.
        project_name:        Project name.
        gl_lines:            List of GL-coded line items.
        compliance_summary:  Human-readable compliance status.
        is_reminder:         True if this is a 48hr reminder.
        is_escalation:       True if this is a 96hr escalation.
        thread_ts:           If set, posts as a reply in thread.

    Returns:
        Slack API response dict with 'ts' (message timestamp) for threading.
    """
    channel = os.getenv("SLACK_APPROVAL_CHANNEL", "")
    if not channel:
        logger.warning("SLACK_APPROVAL_CHANNEL not configured.")
        return {"ok": False, "error": "channel_missing"}

    tier_key = _get_tier(amount)
    tier_label = APPROVAL_TIERS[tier_key]["label"]

    blocks = _build_approval_blocks(
        bill_id=bill_id,
        invoice_number=invoice_number,
        vendor_name=vendor_name,
        amount=amount,
        entity_name=entity_name,
        project_name=project_name,
        gl_lines=gl_lines,
        compliance_summary=compliance_summary,
        tier_label=tier_label,
        is_reminder=is_reminder,
        is_escalation=is_escalation,
    )

    payload = {
        "channel": channel,
        "text": f"{'[ESCALATION] ' if is_escalation else ''}Approval needed: {invoice_number} from {vendor_name} — ${amount:,.2f}",
        "blocks": blocks,
    }

    if thread_ts:
        payload["thread_ts"] = thread_ts

    result = _slack_post("chat.postMessage", payload)

    if result.get("ok"):
        logger.info(
            f"Slack approval message sent | "
            f"bill_id={bill_id} | invoice={invoice_number} | "
            f"amount=${amount:,.2f} | tier={tier_label} | "
            f"reminder={is_reminder} | escalation={is_escalation}"
        )
    return result


# ---------------------------------------------------------------------------
# Update message after decision
# ---------------------------------------------------------------------------

def update_approval_message(
    channel: str,
    message_ts: str,
    bill_id: str,
    invoice_number: str,
    vendor_name: str,
    amount: float,
    decision: str,
    decided_by: str,
) -> dict:
    """
    Update the original Slack message after approve/reject/hold decision.
    Replaces the buttons with a decision summary.
    """
    emoji = {"approve": "✅", "reject": "❌", "hold": "⏸"}.get(decision, "•")
    label = {"approve": "APPROVED", "reject": "REJECTED", "hold": "ON HOLD"}.get(decision, decision.upper())

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"{emoji} *{label}* by {decided_by}\n"
                    f"Invoice *{invoice_number}* from *{vendor_name}* — "
                    f"*${amount:,.2f}*\n"
                    f"_{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_"
                )
            }
        }
    ]

    channel_id = channel or os.getenv("SLACK_APPROVAL_CHANNEL", "")
    return _slack_post("chat.update", {
        "channel": channel_id,
        "ts": message_ts,
        "blocks": blocks,
        "text": f"{label}: {invoice_number} from {vendor_name}",
    })


# ---------------------------------------------------------------------------
# Post simple notification
# ---------------------------------------------------------------------------

def post_notification(message: str, channel: Optional[str] = None) -> dict:
    """Post a plain text notification to the approval channel."""
    channel_id = channel or os.getenv("SLACK_APPROVAL_CHANNEL", "")
    return _slack_post("chat.postMessage", {
        "channel": channel_id,
        "text": message,
    })


# ---------------------------------------------------------------------------
# Webhook signature verification
# ---------------------------------------------------------------------------

def verify_slack_signature(
    signing_secret: str,
    request_body: bytes,
    timestamp: str,
    signature: str,
) -> bool:
    """
    Verify Slack request signature using HMAC-SHA256.
    Prevents replay attacks and unauthorized requests.
    """
    if not signing_secret:
        logger.warning("SLACK_SIGNING_SECRET not configured. Skipping verification.")
        return False

    base = f"v0:{timestamp}:{request_body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        base.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------

def get_slack_client():
    """Return a dict of Slack functions for use in other modules."""
    return {
        "send_approval": send_approval_message,
        "update_approval": update_approval_message,
        "notify": post_notification,
    }
