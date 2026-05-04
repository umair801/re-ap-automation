# integrations/gmail_client.py

from __future__ import annotations

import base64
import email as email_lib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from openai import OpenAI

from core.config import get_settings
from core.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CLASSIFICATION_PROMPT = """
You are an Accounts Payable document classifier.

Given this email subject and body, classify the document type.
Return only one of these exact strings:
- invoice
- credit_memo
- statement
- other

Subject: {subject}

Body (first 500 chars):
{body}

Return only the classification word. Nothing else.
"""


@dataclass
class EmailAttachment:
    filename: str
    content_type: str
    data: bytes
    size_bytes: int


@dataclass
class InvoiceEmail:
    message_id: str
    sender: str
    subject: str
    received_at: datetime
    body_text: str
    classification: str
    attachments: list[EmailAttachment] = field(default_factory=list)


def _build_gmail_service():
    """
    Build and return an authenticated Gmail API service client.
    Uses OAuth2 refresh token from environment variables.
    """
    creds = Credentials(
        token=None,
        refresh_token=settings.gmail_refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.gmail_client_id,
        client_secret=settings.gmail_client_secret,
        scopes=GMAIL_SCOPES,
    )

    # Refresh to get a valid access token
    creds.refresh(Request())
    service = build("gmail", "v1", credentials=creds)
    return service


def _classify_email(subject: str, body: str) -> str:
    """
    Use GPT-4o to classify an email as invoice, credit_memo,
    statement, or other.
    """
    try:
        openai_client = OpenAI(api_key=settings.openai_api_key)
        prompt = CLASSIFICATION_PROMPT.format(
            subject=subject,
            body=body[:500],
        )
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=10,
        )
        classification = response.choices[0].message.content.strip().lower()

        valid = {"invoice", "credit_memo", "statement", "other"}
        if classification not in valid:
            classification = "other"

        return classification

    except Exception as e:
        logger.warning("Email classification failed", error=str(e))
        return "other"


def _extract_body_text(payload: dict) -> str:
    """
    Recursively extract plain text body from a Gmail message payload.
    Handles both simple and multipart message structures.
    """
    body_text = ""

    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            body_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    elif mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            body_text += _extract_body_text(part)

    return body_text


def _extract_attachments(
    service,
    user_id: str,
    message_id: str,
    payload: dict,
) -> list[EmailAttachment]:
    """
    Extract all PDF attachments from a Gmail message payload.
    Downloads attachment data using the Gmail API.
    """
    attachments: list[EmailAttachment] = []

    parts = payload.get("parts", [])

    for part in parts:
        filename = part.get("filename", "")
        mime_type = part.get("mimeType", "")

        # Only process PDF attachments
        if not filename.lower().endswith(".pdf") and "pdf" not in mime_type:
            continue

        attachment_id = part.get("body", {}).get("attachmentId")
        if not attachment_id:
            continue

        try:
            attachment_data = (
                service.users()
                .messages()
                .attachments()
                .get(userId=user_id, messageId=message_id, id=attachment_id)
                .execute()
            )

            raw_data = attachment_data.get("data", "")
            pdf_bytes = base64.urlsafe_b64decode(raw_data)

            attachments.append(
                EmailAttachment(
                    filename=filename,
                    content_type=mime_type,
                    data=pdf_bytes,
                    size_bytes=len(pdf_bytes),
                )
            )

            logger.info(
                "Attachment downloaded",
                filename=filename,
                size_bytes=len(pdf_bytes),
                message_id=message_id,
            )

        except Exception as e:
            logger.error(
                "Failed to download attachment",
                filename=filename,
                message_id=message_id,
                error=str(e),
            )

    return attachments


def fetch_invoice_emails(
    max_results: int = 20,
    query: str = "has:attachment filename:pdf subject:invoice",
) -> list[InvoiceEmail]:
    """
    Fetch emails from Gmail that likely contain invoice attachments.

    Args:
        max_results: Maximum number of emails to fetch per run.
        query: Gmail search query to filter relevant emails.

    Returns:
        List of InvoiceEmail objects with attachments and classification.
    """
    try:
        service = _build_gmail_service()
    except Exception as e:
        logger.error("Gmail authentication failed", error=str(e))
        return []

    user_id = "me"
    invoice_emails: list[InvoiceEmail] = []

    try:
        results = (
            service.users()
            .messages()
            .list(userId=user_id, q=query, maxResults=max_results)
            .execute()
        )

        messages = results.get("messages", [])
        logger.info("Gmail messages found", count=len(messages), query=query)

        for msg_ref in messages:
            message_id = msg_ref["id"]

            try:
                message = (
                    service.users()
                    .messages()
                    .get(userId=user_id, id=message_id, format="full")
                    .execute()
                )

                headers = {
                    h["name"].lower(): h["value"]
                    for h in message.get("payload", {}).get("headers", [])
                }

                sender = headers.get("from", "")
                subject = headers.get("subject", "")
                date_str = headers.get("date", "")

                # Parse received date
                try:
                    from email.utils import parsedate_to_datetime
                    received_at = parsedate_to_datetime(date_str)
                except Exception:
                    received_at = datetime.utcnow()

                payload = message.get("payload", {})
                body_text = _extract_body_text(payload)
                attachments = _extract_attachments(
                    service, user_id, message_id, payload
                )

                # Only process emails that have PDF attachments
                if not attachments:
                    continue

                classification = _classify_email(subject, body_text)

                invoice_email = InvoiceEmail(
                    message_id=message_id,
                    sender=sender,
                    subject=subject,
                    received_at=received_at,
                    body_text=body_text,
                    classification=classification,
                    attachments=attachments,
                )

                invoice_emails.append(invoice_email)

                logger.info(
                    "Email processed",
                    message_id=message_id,
                    sender=sender,
                    classification=classification,
                    attachments=len(attachments),
                )

            except Exception as e:
                logger.error(
                    "Failed to process email",
                    message_id=message_id,
                    error=str(e),
                )

    except Exception as e:
        logger.error("Gmail fetch failed", error=str(e))

    return invoice_emails


def mark_email_processed(message_id: str, label: str = "AP-Processed") -> bool:
    """
    Apply a Gmail label to a processed email to prevent reprocessing.
    Creates the label if it does not exist.

    Returns True on success, False on failure.
    """
    try:
        service = _build_gmail_service()
        user_id = "me"

        # Get or create label
        labels_result = service.users().labels().list(userId=user_id).execute()
        existing = {
            l["name"]: l["id"] for l in labels_result.get("labels", [])
        }

        if label not in existing:
            new_label = (
                service.users()
                .labels()
                .create(userId=user_id, body={"name": label})
                .execute()
            )
            label_id = new_label["id"]
            logger.info("Gmail label created", label=label)
        else:
            label_id = existing[label]

        # Apply label to message
        service.users().messages().modify(
            userId=user_id,
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

        logger.info("Email labeled as processed", message_id=message_id)
        return True

    except Exception as e:
        logger.error("Failed to label email", message_id=message_id, error=str(e))
        return False