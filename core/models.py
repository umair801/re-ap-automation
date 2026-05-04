# core/models.py

from __future__ import annotations
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import Optional, List
from uuid import UUID, uuid4
from pydantic import BaseModel, Field


# ─── Enums ────────────────────────────────────────────────────────────────────

class InvoiceStatus(str, Enum):
    RECEIVED = "received"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    VALIDATING = "validating"
    VALIDATED = "validated"
    MATCHING = "matching"
    MATCHED = "matched"
    PARTIAL_MATCH = "partial_match"
    MISMATCH = "mismatch"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXCEPTION = "exception"
    SCHEDULED = "scheduled"
    PAID = "paid"
    SYNCED = "synced"
    FAILED = "failed"


class MatchStatus(str, Enum):
    FULL_MATCH = "full_match"
    PARTIAL_MATCH = "partial_match"
    MISMATCH = "mismatch"
    PO_NOT_FOUND = "po_not_found"
    RECEIPT_NOT_FOUND = "receipt_not_found"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    TIMED_OUT = "timed_out"


class PaymentStatus(str, Enum):
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IngestionSource(str, Enum):
    EMAIL = "email"
    PDF_UPLOAD = "pdf_upload"
    EDI = "edi"


class ERPTarget(str, Enum):
    QUICKBOOKS = "quickbooks"
    XERO = "xero"
    SAP = "sap"
    NETSUITE = "netsuite"
    NONE = "none"


class ExceptionType(str, Enum):
    VALIDATION_FAILED = "validation_failed"
    MATCH_FAILED = "match_failed"
    DUPLICATE_INVOICE = "duplicate_invoice"
    VENDOR_NOT_FOUND = "vendor_not_found"
    AMOUNT_TOLERANCE_EXCEEDED = "amount_tolerance_exceeded"
    MISSING_PO = "missing_po"
    EXTRACTION_FAILED = "extraction_failed"
    ERP_SYNC_FAILED = "erp_sync_failed"


# ─── Sub-models ───────────────────────────────────────────────────────────────

class LineItem(BaseModel):
    model_config = {"validate_assignment": True}
    line_number: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    total: Decimal
    po_line_number: Optional[int] = None
    gl_account: Optional[str] = None


class VendorInfo(BaseModel):
    model_config = {"validate_assignment": True}
    vendor_id: Optional[str] = None
    vendor_name: str
    vendor_email: Optional[str] = None
    vendor_phone: Optional[str] = None
    vendor_address: Optional[str] = None
    payment_terms: Optional[str] = None


class ValidationError(BaseModel):
    model_config = {"validate_assignment": True}
    field: str
    error_code: str
    message: str


class MatchDetail(BaseModel):
    model_config = {"validate_assignment": True}
    field: str
    invoice_value: str
    po_value: Optional[str] = None
    receipt_value: Optional[str] = None
    within_tolerance: bool
    variance_percent: Optional[Decimal] = None


# ─── Core Models ──────────────────────────────────────────────────────────────

class Invoice(BaseModel):
    model_config = {"validate_assignment": True}
    id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Ingestion metadata
    source: IngestionSource
    raw_file_path: Optional[str] = None
    email_message_id: Optional[str] = None

    # Extracted fields
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    vendor: Optional[VendorInfo] = None
    po_number: Optional[str] = None
    currency: str = "USD"
    subtotal: Optional[Decimal] = None
    tax: Optional[Decimal] = None
    total: Optional[Decimal] = None
    line_items: List[LineItem] = Field(default_factory=list)
    payment_terms: Optional[str] = None
    notes: Optional[str] = None

    # Workflow state
    status: InvoiceStatus = InvoiceStatus.RECEIVED
    validation_errors: List[ValidationError] = Field(default_factory=list)
    match_status: Optional[MatchStatus] = None
    match_details: List[MatchDetail] = Field(default_factory=list)
    approval_id: Optional[UUID] = None
    payment_id: Optional[UUID] = None
    erp_target: ERPTarget = ERPTarget.NONE
    erp_transaction_id: Optional[str] = None

    # Extraction confidence
    extraction_confidence: Optional[float] = None
    extraction_model: str = "gpt-4o"


class PurchaseOrder(BaseModel):
    model_config = {"validate_assignment": True}
    id: UUID = Field(default_factory=uuid4)
    po_number: str
    vendor_id: Optional[str] = None
    vendor_name: str
    po_date: date
    total_amount: Decimal
    currency: str = "USD"
    line_items: List[LineItem] = Field(default_factory=list)
    status: str = "open"
    erp_id: Optional[str] = None


class GoodsReceipt(BaseModel):
    model_config = {"validate_assignment": True}
    id: UUID = Field(default_factory=uuid4)
    receipt_number: str
    po_number: str
    receipt_date: date
    line_items: List[LineItem] = Field(default_factory=list)
    received_by: Optional[str] = None
    erp_id: Optional[str] = None


class ApprovalRecord(BaseModel):
    model_config = {"validate_assignment": True}
    id: UUID = Field(default_factory=uuid4)
    invoice_id: UUID
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    approver_name: str
    approver_email: str
    approver_phone: Optional[str] = None
    approval_token: str
    amount: Decimal
    status: ApprovalStatus = ApprovalStatus.PENDING
    decided_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    escalated_to: Optional[str] = None
    timeout_at: Optional[datetime] = None


class PaymentRecord(BaseModel):
    model_config = {"validate_assignment": True}
    id: UUID = Field(default_factory=uuid4)
    invoice_id: UUID
    created_at: datetime = Field(default_factory=datetime.utcnow)

    vendor_name: str
    amount: Decimal
    currency: str = "USD"
    scheduled_date: date
    payment_method: Optional[str] = None
    batch_id: Optional[str] = None
    status: PaymentStatus = PaymentStatus.SCHEDULED
    erp_payment_id: Optional[str] = None
    completed_at: Optional[datetime] = None


class ERPProvider(str, Enum):
    QUICKBOOKS = "quickbooks"
    XERO = "xero"
    SAP = "sap"
    NETSUITE = "netsuite"


class ERPSyncResult(BaseModel):
    model_config = {"validate_assignment": True}

    invoice_number: str
    erp_provider: ERPProvider
    success: bool
    erp_transaction_id: Optional[str] = None
    error_message: Optional[str] = None

    
class AuditEntry(BaseModel):
    model_config = {"validate_assignment": True}
    id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    invoice_id: UUID
    agent: str
    action: str
    from_status: Optional[str] = None
    to_status: Optional[str] = None
    detail: Optional[str] = None
    success: bool = True
    error_message: Optional[str] = None


class ExceptionRecord(BaseModel):
    model_config = {"validate_assignment": True}
    id: UUID = Field(default_factory=uuid4)
    invoice_id: UUID
    created_at: datetime = Field(default_factory=datetime.utcnow)

    exception_type: ExceptionType
    description: str
    vendor_notified: bool = False
    vendor_notification_sent_at: Optional[datetime] = None
    resolved: bool = False
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolution_notes: Optional[str] = None


# ─── Order Models (Track B) ───────────────────────────────────────────────────────────────

class OrderStatus(str, Enum):
    RECEIVED       = "received"
    EXTRACTING     = "extracting"
    EXTRACTED      = "extracted"
    REVIEW_NEEDED  = "review_needed"   # confidence below threshold
    VALIDATED      = "validated"
    SYNCING        = "syncing"
    SYNCED         = "synced"
    FAILED         = "failed"


class OrderSource(str, Enum):
    EMAIL          = "email"
    EMAIL_ATTACHMENT = "email_attachment"
    PDF_UPLOAD     = "pdf_upload"


class OrderLanguage(str, Enum):
    ENGLISH        = "en"
    FRENCH         = "fr"
    UNKNOWN        = "unknown"


class OrderLineItem(BaseModel):
    model_config = {"validate_assignment": True}
    line_number:       int
    sku:               Optional[str]     = None
    description:       str
    quantity:          Decimal
    unit_price:        Optional[Decimal] = None
    total:             Optional[Decimal] = None
    unit_of_measure:   Optional[str]     = None
    delivery_date:     Optional[date]    = None
    customer_line_ref: Optional[str]     = None   # buyer's own line reference


class CustomerInfo(BaseModel):
    model_config = {"validate_assignment": True}
    customer_name:    str
    customer_id:      Optional[str] = None
    customer_email:   Optional[str] = None
    customer_phone:   Optional[str] = None
    customer_address: Optional[str] = None
    contact_person:   Optional[str] = None


class SalesOrder(BaseModel):
    model_config = {"validate_assignment": True}
    id:           UUID     = Field(default_factory=uuid4)
    created_at:   datetime = Field(default_factory=datetime.utcnow)
    updated_at:   datetime = Field(default_factory=datetime.utcnow)

    # Ingestion metadata
    source:            OrderSource
    email_message_id:  Optional[str] = None
    raw_file_path:     Optional[str] = None
    detected_language: OrderLanguage = OrderLanguage.UNKNOWN

    # Extracted fields
    order_number:       Optional[str]          = None
    order_date:         Optional[date]         = None
    requested_delivery: Optional[date]         = None
    customer:           Optional[CustomerInfo] = None
    customer_po_ref:    Optional[str]          = None   # buyer's PO reference
    currency:           str                    = "USD"
    subtotal:           Optional[Decimal]      = None
    tax:                Optional[Decimal]      = None
    total:              Optional[Decimal]      = None
    line_items:         List[OrderLineItem]    = Field(default_factory=list)
    shipping_address:   Optional[str]          = None
    billing_address:    Optional[str]          = None
    payment_terms:      Optional[str]          = None
    notes:              Optional[str]          = None

    # Workflow state
    status:              OrderStatus    = OrderStatus.RECEIVED
    extraction_confidence: Optional[float] = None
    extraction_model:    str            = "gpt-4o"
    review_reasons:      List[str]      = Field(default_factory=list)
    erp_sync_id:         Optional[str]  = None
    erp_sync_error:      Optional[str]  = None


class OrderReviewItem(BaseModel):
    """Human review queue entry for low-confidence order extractions."""
    model_config = {"validate_assignment": True}
    id:           UUID     = Field(default_factory=uuid4)
    order_id:     UUID
    created_at:   datetime = Field(default_factory=datetime.utcnow)
    confidence:   float
    reasons:      List[str]
    raw_text:     Optional[str] = None
    resolved:     bool          = False
    resolved_at:  Optional[datetime] = None
    resolved_by:  Optional[str]      = None