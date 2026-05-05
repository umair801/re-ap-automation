# integrations/dropbox_client.py
# Dropbox document storage for AP Automation System
# Stores vendor docs, generated indemnities, invoices, and CC receipts
# Two folder structures: vendor-cut and project-cut

import os
import logging
from pathlib import Path
from typing import Optional
import dropbox
from dropbox.exceptions import ApiError, AuthError
from dropbox.files import WriteMode
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Folder structure constants (per spec)
# ---------------------------------------------------------------------------

# Vendor-cut structure
# /AP-Automation/Vendors/{vendor_name}/W9/
# /AP-Automation/Vendors/{vendor_name}/MasterPolicy/
# /AP-Automation/Vendors/{vendor_name}/COI/{project_name}/
# /AP-Automation/Vendors/{vendor_name}/Indemnity/{project_name}/

# Project-cut structure
# /AP-Automation/Projects/{project_name}/Invoices/
# /AP-Automation/Projects/{project_name}/CCReceipts/{year}/{month}/
# /AP-Automation/Projects/{project_name}/Compliance/{vendor_name}/

# CC Receipt structure (by card owner)
# /AP-Automation/CCReceipts/{card_owner}/{card_last_4}/{year}/{month}/

# IIF exports
# /AP-Automation/IIF/{entity_name}/{date}.iif

# Generated indemnities (audit trail)
# /AP-Automation/GeneratedIndemnities/{project_name}/{vendor_name}/


class DropboxClient:
    """
    Dropbox storage client for AP Automation System.
    Handles upload, download, and folder management for all document types.
    """

    def __init__(self):
        self.access_token = os.getenv("DROPBOX_ACCESS_TOKEN", "")
        self.root_folder = os.getenv("DROPBOX_ROOT_FOLDER", "/AP-Automation").rstrip("/")
        self._dbx: Optional[dropbox.Dropbox] = None

    def _get_client(self) -> dropbox.Dropbox:
        """Return authenticated Dropbox client."""
        if self._dbx is None:
            if not self.access_token:
                raise ValueError("DROPBOX_ACCESS_TOKEN not configured.")
            self._dbx = dropbox.Dropbox(self.access_token)
            # Verify token
            try:
                self._dbx.users_get_current_account()
                logger.info("Dropbox authentication successful.")
            except AuthError as e:
                self._dbx = None
                raise ValueError(f"Dropbox auth failed: {e}")
        return self._dbx

    # -----------------------------------------------------------------------
    # Core upload/download
    # -----------------------------------------------------------------------

    def upload_file(
        self,
        local_path: str,
        dropbox_path: str,
        overwrite: bool = True,
    ) -> dict:
        """
        Upload a local file to Dropbox.

        Args:
            local_path:    Path to local file.
            dropbox_path:  Full Dropbox path including filename.
            overwrite:     If True, overwrite existing file.

        Returns:
            {'success': bool, 'path': str, 'size': int}
        """
        dbx = self._get_client()
        local = Path(local_path)

        if not local.exists():
            logger.error(f"Local file not found: {local_path}")
            return {"success": False, "error": f"File not found: {local_path}"}

        full_path = self._full_path(dropbox_path)
        mode = WriteMode.overwrite if overwrite else WriteMode.add

        try:
            with open(local_path, "rb") as f:
                metadata = dbx.files_upload(f.read(), full_path, mode=mode)

            logger.info(f"Uploaded to Dropbox: {full_path} ({metadata.size} bytes)")
            return {
                "success": True,
                "path": metadata.path_display,
                "size": metadata.size,
            }
        except ApiError as e:
            logger.error(f"Dropbox upload failed: {e}")
            return {"success": False, "error": str(e)}

    def upload_bytes(
        self,
        content: bytes,
        dropbox_path: str,
        overwrite: bool = True,
    ) -> dict:
        """Upload bytes directly to Dropbox (no local file needed)."""
        dbx = self._get_client()
        full_path = self._full_path(dropbox_path)
        mode = WriteMode.overwrite if overwrite else WriteMode.add

        try:
            metadata = dbx.files_upload(content, full_path, mode=mode)
            logger.info(f"Uploaded bytes to Dropbox: {full_path} ({metadata.size} bytes)")
            return {"success": True, "path": metadata.path_display, "size": metadata.size}
        except ApiError as e:
            logger.error(f"Dropbox bytes upload failed: {e}")
            return {"success": False, "error": str(e)}

    def download_file(self, dropbox_path: str, local_path: str) -> dict:
        """Download a file from Dropbox to local path."""
        dbx = self._get_client()
        full_path = self._full_path(dropbox_path)

        try:
            metadata, response = dbx.files_download(full_path)
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            with open(local_path, "wb") as f:
                f.write(response.content)
            logger.info(f"Downloaded from Dropbox: {full_path} → {local_path}")
            return {"success": True, "local_path": local_path, "size": metadata.size}
        except ApiError as e:
            logger.error(f"Dropbox download failed: {e}")
            return {"success": False, "error": str(e)}

    def get_shared_link(self, dropbox_path: str) -> Optional[str]:
        """Get or create a shared link for a file."""
        dbx = self._get_client()
        full_path = self._full_path(dropbox_path)

        try:
            # Try to get existing shared link first
            links = dbx.sharing_list_shared_links(path=full_path)
            if links.links:
                return links.links[0].url
            # Create new shared link
            link = dbx.sharing_create_shared_link_with_settings(full_path)
            return link.url
        except ApiError as e:
            logger.warning(f"Could not get shared link for {full_path}: {e}")
            return None

    # -----------------------------------------------------------------------
    # Vendor-cut document storage
    # -----------------------------------------------------------------------

    def store_w9(self, vendor_name: str, local_path: str) -> dict:
        """Store W-9 in vendor-cut folder."""
        safe_vendor = self._safe_name(vendor_name)
        filename = Path(local_path).name
        dropbox_path = f"/Vendors/{safe_vendor}/W9/{filename}"
        return self.upload_file(local_path, dropbox_path)

    def store_master_policy(self, vendor_name: str, local_path: str) -> dict:
        """Store Master Insurance Policy in vendor-cut folder."""
        safe_vendor = self._safe_name(vendor_name)
        filename = Path(local_path).name
        dropbox_path = f"/Vendors/{safe_vendor}/MasterPolicy/{filename}"
        return self.upload_file(local_path, dropbox_path)

    def store_coi(self, vendor_name: str, project_name: str, local_path: str) -> dict:
        """Store Certificate of Insurance in vendor-cut and project-cut folders."""
        safe_vendor = self._safe_name(vendor_name)
        safe_project = self._safe_name(project_name)
        filename = Path(local_path).name

        # Vendor-cut
        vendor_path = f"/Vendors/{safe_vendor}/COI/{safe_project}/{filename}"
        result = self.upload_file(local_path, vendor_path)

        # Project-cut
        project_path = f"/Projects/{safe_project}/Compliance/{safe_vendor}/COI/{filename}"
        self.upload_file(local_path, project_path)

        return result

    def store_indemnity(self, vendor_name: str, project_name: str, local_path: str) -> dict:
        """Store Indemnity Agreement in vendor-cut and project-cut folders."""
        safe_vendor = self._safe_name(vendor_name)
        safe_project = self._safe_name(project_name)
        filename = Path(local_path).name

        # Vendor-cut
        vendor_path = f"/Vendors/{safe_vendor}/Indemnity/{safe_project}/{filename}"
        result = self.upload_file(local_path, vendor_path)

        # Project-cut (audit trail)
        project_path = f"/GeneratedIndemnities/{safe_project}/{safe_vendor}/{filename}"
        self.upload_file(local_path, project_path)

        return result

    # -----------------------------------------------------------------------
    # Invoice and CC receipt storage
    # -----------------------------------------------------------------------

    def store_invoice(
        self, project_name: str, vendor_name: str, invoice_number: str, local_path: str
    ) -> dict:
        """Store vendor invoice PDF in project-cut folder."""
        safe_project = self._safe_name(project_name)
        safe_vendor = self._safe_name(vendor_name)
        filename = Path(local_path).name
        dropbox_path = f"/Projects/{safe_project}/Invoices/{safe_vendor}/{filename}"
        return self.upload_file(local_path, dropbox_path)

    def store_cc_receipt(
        self,
        card_owner_entity: str,
        card_last_4: str,
        charge_date,
        local_path: str,
    ) -> dict:
        """
        Store CC receipt PDF organized by Card Owner → Card Last 4 → Year → Month.
        Per spec: backup audit copy (QBO transaction is the official record).
        """
        safe_entity = self._safe_name(card_owner_entity)
        year = str(charge_date.year)
        month = f"{charge_date.month:02d}"
        filename = Path(local_path).name
        dropbox_path = f"/CCReceipts/{safe_entity}/{card_last_4}/{year}/{month}/{filename}"
        return self.upload_file(local_path, dropbox_path)

    # -----------------------------------------------------------------------
    # IIF export storage
    # -----------------------------------------------------------------------

    def store_iif_export(self, entity_name: str, local_path: str) -> dict:
        """Store daily IIF export file in Dropbox for backup."""
        safe_entity = self._safe_name(entity_name)
        filename = Path(local_path).name
        dropbox_path = f"/IIF/{safe_entity}/{filename}"
        return self.upload_file(local_path, dropbox_path)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _full_path(self, relative_path: str) -> str:
        """Combine root folder with relative path."""
        relative = relative_path.lstrip("/")
        return f"{self.root_folder}/{relative}"

    def _safe_name(self, name: str) -> str:
        """Make a name safe for use as a Dropbox folder name."""
        return "".join(
            c if c.isalnum() or c in "- _." else "_"
            for c in name
        ).strip("_").strip()

    def health_check(self) -> dict:
        """Verify Dropbox connection and token validity."""
        try:
            dbx = self._get_client()
            account = dbx.users_get_current_account()
            return {
                "status": "ok",
                "account": account.email,
                "root_folder": self.root_folder,
            }
        except Exception as e:
            return {"status": "error", "detail": str(e)}


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_client: Optional[DropboxClient] = None


def get_dropbox_client() -> DropboxClient:
    """Return the shared DropboxClient singleton."""
    global _client
    if _client is None:
        _client = DropboxClient()
    return _client
