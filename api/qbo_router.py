# api/qbo_router.py
# QuickBooks Online OAuth 2.0 setup and health check endpoints
# One-time OAuth flow to get refresh token, then stored in .env

import os
import logging
import base64
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/qbo", tags=["QuickBooks Online"])

QBO_AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
QBO_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_SCOPES = "com.intuit.quickbooks.accounting"
REDIRECT_URI = os.getenv("QBO_REDIRECT_URI", "https://ap-re.datawebify.com/qbo/callback")


# ---------------------------------------------------------------------------
# Step 1: Start OAuth flow
# GET /qbo/connect
# ---------------------------------------------------------------------------

@router.get("/connect", summary="Start QBO OAuth authorization flow")
async def qbo_connect():
    """
    Redirects to Intuit authorization page.
    Visit this URL once to authorize the app and get a refresh token.
    """
    client_id = os.getenv("QUICKBOOKS_CLIENT_ID", "")
    if not client_id:
        return HTMLResponse(
            "<h2>Error: QUICKBOOKS_CLIENT_ID not configured in environment.</h2>",
            status_code=500,
        )

    import secrets
    state = secrets.token_urlsafe(16)

    auth_url = (
        f"{QBO_AUTH_URL}"
        f"?client_id={client_id}"
        f"&response_type=code"
        f"&scope={QBO_SCOPES}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state={state}"
    )

    logger.info(f"Redirecting to QBO OAuth: {auth_url}")
    return RedirectResponse(url=auth_url)


# ---------------------------------------------------------------------------
# Step 2: OAuth callback
# GET /qbo/callback
# ---------------------------------------------------------------------------

@router.get("/callback", summary="QBO OAuth callback — exchanges code for tokens")
async def qbo_callback(request: Request):
    """
    Receives the authorization code from Intuit after user approves.
    Exchanges it for access + refresh tokens.
    Displays the refresh token and realm ID for saving to .env
    """
    code = request.query_params.get("code")
    realm_id = request.query_params.get("realmId")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(
            f"<h2>OAuth Error: {error}</h2>"
            f"<p>Description: {request.query_params.get('error_description', 'No description')}</p>",
            status_code=400,
        )

    if not code or not realm_id:
        return HTMLResponse(
            "<h2>Error: Missing code or realmId in callback.</h2>",
            status_code=400,
        )

    # Exchange code for tokens
    client_id = os.getenv("QUICKBOOKS_CLIENT_ID", "")
    client_secret = os.getenv("QUICKBOOKS_CLIENT_SECRET", "")

    credentials = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()

    try:
        response = httpx.post(
            QBO_TOKEN_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            timeout=15,
        )
        response.raise_for_status()
        tokens = response.json()

        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        expires_in = tokens.get("expires_in", 3600)

        logger.info(f"QBO OAuth success. Realm ID: {realm_id}")

        html = f"""
        <html>
        <head><title>QBO OAuth Success</title></head>
        <body style="font-family: monospace; padding: 40px; background: #1a1a1a; color: #00ff00;">
        <h2>✅ QuickBooks Online Connected Successfully</h2>
        <p>Add these values to your <strong>.env</strong> file and Railway Variables:</p>
        <hr/>
        <pre style="background: #000; padding: 20px; border-radius: 8px;">
QUICKBOOKS_REALM_ID={realm_id}
QUICKBOOKS_REFRESH_TOKEN={refresh_token}
        </pre>
        <hr/>
        <p><strong>Access Token</strong> (expires in {expires_in}s — do NOT store, use refresh token):</p>
        <pre style="background: #000; padding: 10px; border-radius: 8px; word-break: break-all; font-size: 11px;">
{access_token[:50]}...
        </pre>
        <p style="color: #ffaa00;">
        ⚠️ Copy the QUICKBOOKS_REALM_ID and QUICKBOOKS_REFRESH_TOKEN above
        and add them to your .env file immediately. Then restart the server.
        </p>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    except httpx.HTTPStatusError as e:
        logger.error(f"QBO token exchange failed: {e.response.text}")
        return HTMLResponse(
            f"<h2>Token Exchange Failed</h2><pre>{e.response.text}</pre>",
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Health check
# GET /qbo/health
# ---------------------------------------------------------------------------

@router.get("/health", summary="QuickBooks Online connection health check")
async def qbo_health():
    """Check if QBO credentials are configured and token refresh works."""
    client_id = os.getenv("QUICKBOOKS_CLIENT_ID", "")
    realm_id = os.getenv("QUICKBOOKS_REALM_ID", "")
    refresh_token = os.getenv("QUICKBOOKS_REFRESH_TOKEN", "")

    if not all([client_id, realm_id, refresh_token]):
        missing = []
        if not client_id: missing.append("QUICKBOOKS_CLIENT_ID")
        if not realm_id: missing.append("QUICKBOOKS_REALM_ID")
        if not refresh_token: missing.append("QUICKBOOKS_REFRESH_TOKEN")
        return {
            "status": "not_configured",
            "missing": missing,
            "setup_url": "/qbo/connect",
        }

    # Try token refresh
    try:
        from integrations.quickbooks_client import _refresh_access_token
        token = _refresh_access_token()
        return {
            "status": "ok",
            "realm_id": realm_id[:8] + "...",
            "token_refresh": "SUCCESS",
            "sandbox": os.getenv("APP_ENV", "development") != "production",
        }
    except Exception as e:
        return {
            "status": "error",
            "detail": str(e),
            "setup_url": "/qbo/connect",
        }
