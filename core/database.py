# core/database.py

import structlog
from supabase import create_client, Client
from core.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()

_client: Client | None = None


def get_supabase() -> Client:
    """Return a singleton Supabase client."""
    global _client
    if _client is None:
        _client = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_KEY,
        )
        logger.info("supabase_client_initialized", url=settings.SUPABASE_URL)
    return _client


def insert_invoice(data: dict) -> dict:
    client = get_supabase()
    result = client.table("ap_invoices").insert(data).execute()
    return result.data[0] if result.data else {}


def update_invoice_status(invoice_number: str, status: str, **kwargs) -> dict:
    client = get_supabase()
    payload = {"status": status, **kwargs}
    result = (
        client.table("ap_invoices")
        .update(payload)
        .eq("invoice_number", invoice_number)
        .execute()
    )
    return result.data[0] if result.data else {}


def get_existing_invoice_numbers() -> list[str]:
    """Return all invoice numbers already stored in Supabase for duplicate detection."""
    client = get_supabase()
    result = client.table("ap_invoices").select("invoice_number").execute()
    return [
        row["invoice_number"]
        for row in (result.data or [])
        if row.get("invoice_number")
    ]


def insert_audit_entry(data: dict) -> dict:
    client = get_supabase()
    result = client.table("ap_audit_log").insert(data).execute()
    return result.data[0] if result.data else {}


def insert_exception(data: dict) -> dict:
    client = get_supabase()
    result = client.table("ap_exceptions").insert(data).execute()
    return result.data[0] if result.data else {}


def insert_approval(data: dict) -> dict:
    client = get_supabase()
    result = client.table("ap_approval_records").insert(data).execute()
    return result.data[0] if result.data else {}


def update_approval(approval_id: str, data: dict) -> dict:
    client = get_supabase()
    result = (
        client.table("ap_approval_records")
        .update(data)
        .eq("approval_id", approval_id)
        .execute()
    )
    return result.data[0] if result.data else {}


def insert_payment(data: dict) -> dict:
    client = get_supabase()
    result = client.table("ap_payments").insert(data).execute()
    return result.data[0] if result.data else {}


def get_metrics() -> dict:
    client = get_supabase()

    total = client.table("ap_invoices").select("id", count="exact").execute()
    matched = (
        client.table("ap_invoices")
        .select("id", count="exact")
        .eq("status", "matched")
        .execute()
    )
    exceptions = (
        client.table("ap_exceptions")
        .select("id", count="exact")
        .eq("resolved", False)
        .execute()
    )
    paid = (
        client.table("ap_invoices")
        .select("id", count="exact")
        .eq("status", "paid")
        .execute()
    )

    return {
        "invoices_processed": total.count or 0,
        "invoices_matched": matched.count or 0,
        "invoices_paid": paid.count or 0,
        "open_exceptions": exceptions.count or 0,
    }