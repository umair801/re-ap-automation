# integrations/quickbooks_client.py

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from core.config import get_settings
from core.logger import get_logger
from core.models import Invoice, PaymentRecord

logger = get_logger(__name__)
settings = get_settings()

# QuickBooks Online API base URLs
QBO_BASE_URL = "https://quickbooks.api.intuit.com/v3/company"
QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com/v3/company"


class QuickBooksAuthError(Exception):
    pass


class QuickBooksAPIError(Exception):
    pass


# ─── Token Management ─────────────────────────────────────────────────────────

def _refresh_access_token() -> str:
    """
    Refresh the QuickBooks OAuth2 access token using the stored refresh token.
    Returns a fresh access token.
    """
    import base64

    credentials = base64.b64encode(
        f"{settings.quickbooks_client_id}:{settings.quickbooks_client_secret}".encode()
    ).decode()

    try:
        response = httpx.post(
            QBO_TOKEN_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": settings.quickbooks_refresh_token,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        access_token = data.get("access_token")

        if not access_token:
            raise QuickBooksAuthError("No access token in refresh response")

        logger.info("QuickBooks token refreshed successfully")
        return access_token

    except httpx.HTTPStatusError as e:
        logger.error("QuickBooks token refresh failed", error=str(e))
        raise QuickBooksAuthError(f"Token refresh failed: {e}")


def _get_headers(access_token: str) -> dict:
    """Build standard QuickBooks API request headers."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get_base_url() -> str:
    """Return sandbox or production base URL based on environment."""
    if settings.app_env == "production":
        return f"{QBO_BASE_URL}/{settings.quickbooks_realm_id}"
    return f"{QBO_SANDBOX_BASE}/{settings.quickbooks_realm_id}"


# ─── Vendor Operations ────────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def find_or_create_vendor(
    vendor_name: str,
    vendor_email: Optional[str] = None,
    access_token: Optional[str] = None,
) -> dict:
    """
    Find an existing vendor in QuickBooks by name, or create a new one.
    Returns the QuickBooks vendor object.
    """
    token = access_token or _refresh_access_token()
    base_url = _get_base_url()
    headers = _get_headers(token)

    # Search for existing vendor
    try:
        query = f"SELECT * FROM Vendor WHERE DisplayName = '{vendor_name}'"
        response = httpx.get(
            f"{base_url}/query",
            headers=headers,
            params={"query": query},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        vendors = (
            data.get("QueryResponse", {}).get("Vendor", [])
        )

        if vendors:
            logger.info(
                "Vendor found in QuickBooks",
                vendor_name=vendor_name,
                qbo_id=vendors[0].get("Id"),
            )
            return vendors[0]

    except Exception as e:
        logger.warning("Vendor search failed", vendor_name=vendor_name, error=str(e))

    # Create new vendor
    vendor_payload = {
        "DisplayName": vendor_name,
        "PrintOnCheckName": vendor_name,
    }

    if vendor_email:
        vendor_payload["PrimaryEmailAddr"] = {"Address": vendor_email}

    try:
        response = httpx.post(
            f"{base_url}/vendor",
            headers=headers,
            json=vendor_payload,
            timeout=15,
        )
        response.raise_for_status()
        vendor = response.json().get("Vendor", {})

        logger.info(
            "Vendor created in QuickBooks",
            vendor_name=vendor_name,
            qbo_id=vendor.get("Id"),
        )
        return vendor

    except httpx.HTTPStatusError as e:
        logger.error("Failed to create vendor", vendor_name=vendor_name, error=str(e))
        raise QuickBooksAPIError(f"Vendor creation failed: {e}")


# ─── Invoice Operations ───────────────────────────────────────────────────────

def _build_qbo_invoice_payload(
    invoice: Invoice,
    vendor_ref: dict,
) -> dict:
    """
    Map our Invoice model to the QuickBooks Bill payload format.
    QuickBooks uses Bills for vendor invoices in AP workflows.
    """
    # Build line items
    lines = []

    if invoice.line_items:
        for item in invoice.line_items:
            lines.append({
                "Amount": float(item.total),
                "DetailType": "AccountBasedExpenseLineDetail",
                "Description": item.description,
                "AccountBasedExpenseLineDetail": {
                    "AccountRef": {
                        "value": "1",  # Default expense account
                        "name": "Accounts Payable",
                    },
                    "BillableStatus": "NotBillable",
                },
            })
    else:
        # Single line for total when no line items
        lines.append({
            "Amount": float(invoice.total or 0),
            "DetailType": "AccountBasedExpenseLineDetail",
            "Description": f"Invoice {invoice.invoice_number}",
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {
                    "value": "1",
                    "name": "Accounts Payable",
                },
                "BillableStatus": "NotBillable",
            },
        })

    payload = {
        "VendorRef": {
            "value": vendor_ref.get("Id"),
            "name": vendor_ref.get("DisplayName"),
        },
        "DocNumber": invoice.invoice_number,
        "TxnDate": str(invoice.invoice_date or datetime.utcnow().date()),
        "DueDate": str(invoice.due_date or ""),
        "CurrencyRef": {"value": invoice.currency},
        "TotalAmt": float(invoice.total or 0),
        "Line": lines,
        "PrivateNote": f"Created by AP-AI. Source: {invoice.source.value}",
    }

    if invoice.po_number:
        payload["PONumber"] = invoice.po_number

    return payload


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def create_bill_in_quickbooks(
    invoice: Invoice,
    access_token: Optional[str] = None,
) -> dict:
    """
    Create a Bill (vendor invoice) in QuickBooks Online.
    Finds or creates the vendor automatically.
    Returns the created QBO Bill object.
    """
    if not settings.quickbooks_realm_id:
        raise QuickBooksAPIError("QuickBooks realm ID not configured")

    token = access_token or _refresh_access_token()
    base_url = _get_base_url()
    headers = _get_headers(token)

    vendor_name = invoice.vendor.vendor_name if invoice.vendor else "Unknown Vendor"
    vendor_email = invoice.vendor.vendor_email if invoice.vendor else None

    # Find or create vendor
    vendor_ref = find_or_create_vendor(vendor_name, vendor_email, token)

    # Build and post the bill
    payload = _build_qbo_invoice_payload(invoice, vendor_ref)

    try:
        response = httpx.post(
            f"{base_url}/bill",
            headers=headers,
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        bill = response.json().get("Bill", {})

        logger.info(
            "Bill created in QuickBooks",
            invoice_number=invoice.invoice_number,
            qbo_bill_id=bill.get("Id"),
            amount=str(invoice.total),
        )
        return bill

    except httpx.HTTPStatusError as e:
        logger.error(
            "Failed to create bill in QuickBooks",
            invoice_number=invoice.invoice_number,
            status=e.response.status_code,
            error=e.response.text[:500],
        )
        raise QuickBooksAPIError(f"Bill creation failed: {e}")


# ─── Payment Operations ───────────────────────────────────────────────────────

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def create_bill_payment_in_quickbooks(
    payment: PaymentRecord,
    qbo_bill_id: str,
    vendor_ref: dict,
    access_token: Optional[str] = None,
) -> dict:
    """
    Record a Bill Payment in QuickBooks Online.
    Links the payment to the original Bill.
    Returns the created QBO BillPayment object.
    """
    token = access_token or _refresh_access_token()
    base_url = _get_base_url()
    headers = _get_headers(token)

    payload = {
        "VendorRef": {
            "value": vendor_ref.get("Id"),
            "name": vendor_ref.get("DisplayName"),
        },
        "PayType": "Check",
        "TotalAmt": float(payment.amount),
        "TxnDate": str(payment.scheduled_date),
        "CurrencyRef": {"value": payment.currency},
        "Line": [
            {
                "Amount": float(payment.amount),
                "LinkedTxn": [
                    {
                        "TxnId": qbo_bill_id,
                        "TxnType": "Bill",
                    }
                ],
            }
        ],
        "PrivateNote": f"Payment by AP-AI. Batch: {payment.batch_id or 'N/A'}",
    }

    try:
        response = httpx.post(
            f"{base_url}/billpayment",
            headers=headers,
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        bill_payment = response.json().get("BillPayment", {})

        logger.info(
            "Bill payment created in QuickBooks",
            payment_id=str(payment.id),
            qbo_payment_id=bill_payment.get("Id"),
            amount=str(payment.amount),
        )
        return bill_payment

    except httpx.HTTPStatusError as e:
        logger.error(
            "Failed to create bill payment in QuickBooks",
            payment_id=str(payment.id),
            error=e.response.text[:500],
        )
        raise QuickBooksAPIError(f"Bill payment creation failed: {e}")


# ─── CC Charge Push (Path 2) ────────────────────────────────────────────────────

def push_cc_charge_to_qbo(charge) -> dict:
    """
    Push a CC charge to QBO as a Purchase (CreditCardCharge).
    Sets BillableStatus=Billable and CustomerRef for project charges.
    Attaches receipt PDF if available.

    Args:
        charge: CCChargeRecord from cc_charge_agent.

    Returns:
        {'success': bool, 'transaction_id': str, 'note': str}
    """
    if not settings.quickbooks_realm_id:
        return {"success": False, "note": "QuickBooks realm ID not configured."}

    try:
        token = _refresh_access_token()
        base_url = _get_base_url()
        headers = _get_headers(token)

        # Build line detail
        billable_status = "Billable" if charge.billable_status.value == "Billable" else "NotBillable"

        line = {
            "Amount": float(charge.total_amount),
            "DetailType": "AccountBasedExpenseLineDetail",
            "Description": charge.description,
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {
                    "value": charge.gl_account or "1",
                },
                "BillableStatus": billable_status,
            },
        }

        # Set CustomerRef for billable project charges
        if charge.billable_status.value == "Billable" and charge.qbo_customer_id:
            line["AccountBasedExpenseLineDetail"]["CustomerRef"] = {
                "value": charge.qbo_customer_id,
            }

        payload = {
            "PaymentType": "CreditCard",
            "EntityRef": {"name": charge.vendor_name},
            "TxnDate": charge.charge_date.isoformat(),
            "DocNumber": charge.receipt_number,
            "PrivateNote": f"CC charge via AP Automation. Card: ...{charge.card_last_4}",
            "Line": [line],
        }

        response = httpx.post(
            f"{base_url}/purchase",
            headers=headers,
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        purchase = response.json().get("Purchase", {})
        transaction_id = purchase.get("Id", "")

        logger.info(
            "CC charge pushed to QBO",
            receipt_number=charge.receipt_number,
            transaction_id=transaction_id,
            billable=charge.billable_status.value,
        )
        return {"success": True, "transaction_id": transaction_id, "note": ""}

    except httpx.HTTPStatusError as e:
        logger.error("QBO CC charge push failed", error=e.response.text[:500])
        return {"success": False, "transaction_id": "", "note": str(e)}
    except Exception as e:
        logger.error("QBO CC charge push error", error=str(e))
        return {"success": False, "transaction_id": "", "note": str(e)}


def create_intercompany_invoice(
    customer_id: str,
    project_name: str,
    line_items: list,
    total: float,
    period: str,
) -> dict:
    """
    Create a draft Customer Invoice in QBO (Operating Entity → Project LLC).
    Used for month-end intercompany reimbursement.

    Args:
        customer_id:  QBO Customer ID for the project LLC.
        project_name: Project name for memo/description.
        line_items:   List of charge dicts with amount, description, cs_code.
        total:        Total invoice amount.
        period:       YYYY-MM string for the billing period.

    Returns:
        {'invoice_id': str, 'success': bool}
    """
    if not settings.quickbooks_realm_id:
        return {"invoice_id": None, "success": False, "note": "QBO realm ID not configured."}

    if not customer_id:
        return {"invoice_id": None, "success": False, "note": "No QBO Customer ID for project."}

    try:
        token = _refresh_access_token()
        base_url = _get_base_url()
        headers = _get_headers(token)

        qbo_lines = [
            {
                "Amount": item["amount"],
                "DetailType": "SalesItemLineDetail",
                "Description": f"{item.get('vendor_name', '')} - {item.get('description', '')} [{item.get('cs_code', '')}]",
                "SalesItemLineDetail": {
                    "ItemRef": {"name": "Services", "value": "1"},
                },
            }
            for item in line_items
        ]

        payload = {
            "CustomerRef": {"value": customer_id},
            "DocNumber": f"REIMB-{period}",
            "TxnDate": datetime.utcnow().date().isoformat(),
            "PrivateNote": f"Intercompany reimbursement: {project_name} | Period: {period}",
            "Line": qbo_lines,
        }

        response = httpx.post(
            f"{base_url}/invoice",
            headers=headers,
            json=payload,
            timeout=20,
        )
        response.raise_for_status()
        invoice = response.json().get("Invoice", {})
        invoice_id = invoice.get("Id", "")

        logger.info(
            "Intercompany reimbursement invoice created in QBO",
            project=project_name,
            period=period,
            invoice_id=invoice_id,
            total=total,
        )
        return {"invoice_id": invoice_id, "success": True}

    except httpx.HTTPStatusError as e:
        logger.error("QBO intercompany invoice creation failed", error=e.response.text[:500])
        return {"invoice_id": None, "success": False, "note": str(e)}
    except Exception as e:
        logger.error("QBO intercompany invoice error", error=str(e))
        return {"invoice_id": None, "success": False, "note": str(e)}


# ─── Health Check ─────────────────────────────────────────────────────────────

def check_quickbooks_connection() -> bool:
    """
    Verify QuickBooks API connectivity by fetching company info.
    Returns True if connected, False otherwise.
    """
    try:
        token = _refresh_access_token()
        base_url = _get_base_url()
        headers = _get_headers(token)

        response = httpx.get(
            f"{base_url}/companyinfo/{settings.quickbooks_realm_id}",
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        company = response.json().get("CompanyInfo", {})

        logger.info(
            "QuickBooks connection verified",
            company_name=company.get("CompanyName"),
        )
        return True

    except Exception as e:
        logger.error("QuickBooks connection check failed", error=str(e))
        return False