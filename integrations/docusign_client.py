# integrations/docusign_client.py
# DocuSign SDK Integration for AP Automation System
# Handles JWT Grant auth, envelope sending, document download, webhook verification

import os
import base64
import hashlib
import hmac
import json
import time
import logging
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from docusign_esign import (
    ApiClient,
    EnvelopesApi,
    EnvelopeDefinition,
    TemplateRole,
    Document,
    Signer,
    SignHere,
    Tabs,
    Recipients,
)
from docusign_esign.client.api_exception import ApiException

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SANDBOX_BASE_URL = "https://demo.docusign.net/restapi"
PRODUCTION_BASE_URL = "https://www.docusign.net/restapi"
SANDBOX_AUTH_URL = "https://account-d.docusign.com"
PRODUCTION_AUTH_URL = "https://account.docusign.com"

# Maps indemnity template names to .env variable names
TEMPLATE_MAP = {
    "standard_construction": "DOCUSIGN_TEMPLATE_STANDARD_CONSTRUCTION",
    "multifamily": "DOCUSIGN_TEMPLATE_MULTIFAMILY",
    "adaptive_reuse": "DOCUSIGN_TEMPLATE_ADAPTIVE_REUSE",
    "material_supply": "DOCUSIGN_TEMPLATE_MATERIAL_SUPPLY",
    "lending": "DOCUSIGN_TEMPLATE_LENDING",
    "material_default": "DOCUSIGN_TEMPLATE_MATERIAL_DEFAULT",
}

# Reminder and expiration schedule (days)
REMINDER_DAYS = [3, 7, 14]
ENVELOPE_EXPIRY_DAYS = 30


# ---------------------------------------------------------------------------
# DocuSign Client
# ---------------------------------------------------------------------------

class DocuSignClient:
    """
    JWT Grant service-integration client for DocuSign eSignature API.
    One instance per process. Token is cached and refreshed automatically.
    """

    def __init__(self):
        self.integration_key = os.getenv("DOCUSIGN_INTEGRATION_KEY", "")
        self.user_id = os.getenv("DOCUSIGN_USER_ID", "")
        self.account_id = os.getenv("DOCUSIGN_ACCOUNT_ID", "")
        self.private_key_path = os.getenv("DOCUSIGN_PRIVATE_KEY_PATH", "keys/docusign_private.key")
        self.sandbox = os.getenv("DOCUSIGN_SANDBOX", "true").lower() == "true"
        self.connect_secret = os.getenv("DOCUSIGN_CONNECT_SECRET", "")

        self.base_url = SANDBOX_BASE_URL if self.sandbox else PRODUCTION_BASE_URL
        self.auth_url = SANDBOX_AUTH_URL if self.sandbox else PRODUCTION_AUTH_URL

        self._api_client: Optional[ApiClient] = None
        self._token_expiry: float = 0.0

    # -----------------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------------

    def _load_private_key(self) -> bytes:
        """Load RSA private key bytes from file."""
        key_path = Path(self.private_key_path)
        if not key_path.exists():
            raise FileNotFoundError(
                f"DocuSign private key not found at: {key_path.resolve()}"
            )
        return key_path.read_bytes()

    def _get_api_client(self) -> ApiClient:
        """
        Return an authenticated ApiClient, refreshing the JWT token if needed.
        Token lifetime is 1 hour; we refresh 5 minutes before expiry.
        """
        now = time.time()
        if self._api_client is None or now >= self._token_expiry - 300:
            self._api_client = self._authenticate()
        return self._api_client

    def _authenticate(self) -> ApiClient:
        """
        Perform JWT Grant authentication and return a configured ApiClient.
        Raises RuntimeError with consent URL if consent has not been granted.
        """
        api_client = ApiClient()
        api_client.host = self.base_url
        api_client.set_base_path(self.base_url)
        api_client.set_oauth_host_name(
            "account-d.docusign.com" if self.sandbox else "account.docusign.com"
        )

        private_key = self._load_private_key()

        try:
            token_response = api_client.request_jwt_user_token(
                client_id=self.integration_key,
                user_id=self.user_id,
                oauth_host_name=(
                    "account-d.docusign.com" if self.sandbox else "account.docusign.com"
                ),
                private_key_bytes=private_key,
                expires_in=3600,
                scopes=["signature", "impersonation"],
            )
        except ApiException as e:
            body = json.loads(e.body) if e.body else {}
            if body.get("error") == "consent_required":
                consent_url = (
                    f"{self.auth_url}/oauth/auth"
                    f"?response_type=code"
                    f"&scope=signature%20impersonation"
                    f"&client_id={self.integration_key}"
                    f"&redirect_uri=http://localhost:8000/callback"
                )
                raise RuntimeError(
                    f"DocuSign consent required. Open this URL in your browser:\n{consent_url}"
                ) from e
            raise

        access_token = token_response.access_token
        api_client.set_default_header("Authorization", f"Bearer {access_token}")
        self._token_expiry = time.time() + 3600
        logger.info("DocuSign JWT authentication successful.")
        return api_client

    # -----------------------------------------------------------------------
    # Envelope Sending
    # -----------------------------------------------------------------------

    def _get_template_id(self, template_name: str) -> str:
        """Resolve template name to DocuSign Template ID from environment."""
        env_var = TEMPLATE_MAP.get(template_name.lower())
        if not env_var:
            raise ValueError(
                f"Unknown template name: '{template_name}'. "
                f"Valid options: {list(TEMPLATE_MAP.keys())}"
            )
        template_id = os.getenv(env_var, "")
        if not template_id or template_id == "your-template-id":
            raise ValueError(
                f"Template ID not configured for '{template_name}'. "
                f"Set {env_var} in your .env file."
            )
        return template_id

    def send_onboarding_envelope(
        self,
        vendor_email: str,
        vendor_name: str,
        project_name: str,
        template_name: str,
        merge_fields: dict,
        envelope_type: str = "full",
    ) -> str:
        """
        Send a compliance envelope to a vendor.

        Args:
            vendor_email:   Vendor's email address.
            vendor_name:    Vendor's legal name (used in DocuSign signer display).
            project_name:   Project name for subject line and merge fields.
            template_name:  One of the six indemnity template keys.
            merge_fields:   Dict of template tab labels to values for pre-fill.
            envelope_type:  'full' (all 4 docs), 'partial' (COI + Indemnity), 'renewal'.

        Returns:
            envelope_id (str) — store this in Airtable Bills/Vendors record.
        """
        api_client = self._get_api_client()
        envelopes_api = EnvelopesApi(api_client)

        template_id = self._get_template_id(template_name)

        # Build signer with pre-filled tabs
        tabs = self._build_tabs(merge_fields)

        signer = TemplateRole(
            email=vendor_email,
            name=vendor_name,
            role_name="Vendor",
            tabs=tabs,
        )

        # Reminder and expiration settings
        notification = {
            "reminders": {
                "reminderEnabled": "true",
                "reminderDelay": str(REMINDER_DAYS[0]),
                "reminderFrequency": "4",
            },
            "expirations": {
                "expireEnabled": "true",
                "expireAfter": str(ENVELOPE_EXPIRY_DAYS),
                "expireWarn": "3",
            },
        }

        envelope_definition = EnvelopeDefinition(
            status="sent",
            template_id=template_id,
            template_roles=[signer],
            email_subject=(
                f"[AP Automation] Compliance Documents Required — {project_name}"
            ),
            email_blurb=(
                f"Please complete the attached compliance documents for {project_name}. "
                f"This request was sent by an automated accounts payable system."
            ),
            notification=notification,
        )

        try:
            result = envelopes_api.create_envelope(
                account_id=self.account_id,
                envelope_definition=envelope_definition,
            )
            envelope_id = result.envelope_id
            logger.info(
                f"Envelope sent to {vendor_email} for {project_name}. "
                f"Envelope ID: {envelope_id} | Type: {envelope_type}"
            )
            return envelope_id

        except ApiException as e:
            logger.error(f"DocuSign envelope creation failed: {e}")
            raise

    def _build_tabs(self, merge_fields: dict):
        """
        Convert a merge_fields dict into DocuSign pre-filled text tabs.
        Keys are DocuSign tab labels; values are the pre-fill strings.
        """
        from docusign_esign import Tabs, Text
        text_tabs = [
            Text(tab_label=label, value=str(value))
            for label, value in merge_fields.items()
        ]
        return Tabs(text_tabs=text_tabs)

    # -----------------------------------------------------------------------
    # Document Download
    # -----------------------------------------------------------------------

    def download_envelope_documents(
        self,
        envelope_id: str,
        output_dir: str,
    ) -> list[str]:
        """
        Download all completed documents from an envelope.

        Args:
            envelope_id: The DocuSign envelope ID.
            output_dir:  Local directory path to save downloaded PDFs.

        Returns:
            List of file paths for the downloaded documents.
        """
        api_client = self._get_api_client()
        envelopes_api = EnvelopesApi(api_client)
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Get document list
        doc_list = envelopes_api.list_documents(
            account_id=self.account_id,
            envelope_id=envelope_id,
        )

        downloaded_paths = []
        for doc in doc_list.envelope_documents:
            doc_id = doc.document_id
            doc_name = doc.name or f"document_{doc_id}"
            safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in doc_name)
            file_path = output_path / f"{envelope_id}_{safe_name}.pdf"

            pdf_bytes = envelopes_api.get_document(
                account_id=self.account_id,
                envelope_id=envelope_id,
                document_id=doc_id,
            )

            with open(file_path, "wb") as f:
                f.write(pdf_bytes)

            downloaded_paths.append(str(file_path))
            logger.info(f"Downloaded document: {file_path}")

        return downloaded_paths

    # -----------------------------------------------------------------------
    # Webhook Signature Verification
    # -----------------------------------------------------------------------

    def verify_connect_signature(
        self,
        payload_bytes: bytes,
        signature_header: str,
    ) -> bool:
        """
        Verify DocuSign Connect HMAC-SHA256 webhook signature.

        Args:
            payload_bytes:      Raw request body bytes from the webhook POST.
            signature_header:   Value of the X-DocuSign-Signature-1 header.

        Returns:
            True if signature is valid, False otherwise.
        """
        if not self.connect_secret:
            logger.warning(
                "DOCUSIGN_CONNECT_SECRET is not set. Skipping signature verification."
            )
            return False

        expected = base64.b64encode(
            hmac.new(
                self.connect_secret.encode("utf-8"),
                payload_bytes,
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        is_valid = hmac.compare_digest(expected, signature_header)
        if not is_valid:
            logger.warning("DocuSign Connect signature verification FAILED.")
        return is_valid

    # -----------------------------------------------------------------------
    # Envelope Status
    # -----------------------------------------------------------------------

    def get_envelope_status(self, envelope_id: str) -> dict:
        """
        Return envelope status and key timestamps.

        Returns dict with keys: status, sent_date, completed_date, declined_date.
        """
        api_client = self._get_api_client()
        envelopes_api = EnvelopesApi(api_client)

        envelope = envelopes_api.get_envelope(
            account_id=self.account_id,
            envelope_id=envelope_id,
        )

        return {
            "status": envelope.status,
            "sent_date": envelope.sent_date_time,
            "completed_date": envelope.completed_date_time,
            "declined_date": envelope.declined_date_time,
            "envelope_id": envelope_id,
        }

    # -----------------------------------------------------------------------
    # Vendor Refusal Handling
    # -----------------------------------------------------------------------

    def void_envelope(self, envelope_id: str, reason: str = "Vendor declined compliance") -> bool:
        """
        Void an envelope (e.g., when vendor refuses Master Policy).
        The caller is responsible for flagging the vendor record in Airtable.
        """
        api_client = self._get_api_client()
        envelopes_api = EnvelopesApi(api_client)

        try:
            envelope_definition = EnvelopeDefinition(status="voided", voided_reason=reason)
            envelopes_api.update(
                account_id=self.account_id,
                envelope_id=envelope_id,
                envelope=envelope_definition,
            )
            logger.info(f"Envelope {envelope_id} voided. Reason: {reason}")
            return True
        except ApiException as e:
            logger.error(f"Failed to void envelope {envelope_id}: {e}")
            return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: Optional[DocuSignClient] = None


def get_docusign_client() -> DocuSignClient:
    """Return the shared DocuSignClient singleton."""
    global _client
    if _client is None:
        _client = DocuSignClient()
    return _client
