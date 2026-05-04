# core/config.py

from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache


class Settings(BaseSettings):
    # OpenAI
    openai_api_key: str = Field(..., env="OPENAI_API_KEY")

    # Anthropic
    anthropic_api_key: str = Field(default="", env="ANTHROPIC_API_KEY")

    # Supabase
    supabase_url: str = Field(..., env="SUPABASE_URL")
    SUPABASE_KEY: str = Field(..., env="SUPABASE_KEY")

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0", env="REDIS_URL")

    # Gmail
    gmail_client_id: str = Field(default="", env="GMAIL_CLIENT_ID")
    gmail_client_secret: str = Field(default="", env="GMAIL_CLIENT_SECRET")
    gmail_refresh_token: str = Field(default="", env="GMAIL_REFRESH_TOKEN")

    # SendGrid
    sendgrid_api_key: str = Field(default="", env="SENDGRID_API_KEY")
    sendgrid_from_email: str = Field(default="", env="SENDGRID_FROM_EMAIL")

    # Twilio
    twilio_account_sid: str = Field(default="", env="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field(default="", env="TWILIO_AUTH_TOKEN")
    twilio_from_number: str = Field(default="", env="TWILIO_FROM_NUMBER")

    # QuickBooks
    quickbooks_client_id: str = Field(default="", env="QUICKBOOKS_CLIENT_ID")
    quickbooks_client_secret: str = Field(default="", env="QUICKBOOKS_CLIENT_SECRET")
    quickbooks_refresh_token: str = Field(default="", env="QUICKBOOKS_REFRESH_TOKEN")
    quickbooks_realm_id: str = Field(default="", env="QUICKBOOKS_REALM_ID")

    # Xero
    xero_client_id: str = Field(default="", env="XERO_CLIENT_ID")
    xero_client_secret: str = Field(default="", env="XERO_CLIENT_SECRET")
    xero_refresh_token: str = Field(default="", env="XERO_REFRESH_TOKEN")

    # App settings
    app_env: str = Field(default="development", env="APP_ENV")
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    approval_threshold: float = Field(default=5000.00, env="APPROVAL_THRESHOLD")
    approval_timeout_hours: int = Field(default=48, env="APPROVAL_TIMEOUT_HOURS")
    match_tolerance_percent: float = Field(default=2.0, env="MATCH_TOLERANCE_PERCENT")

    model_config = {"env_file": ".env", "extra": "ignore"}

    AP_MANAGER_EMAIL: str = "ap-manager@company.com"
    AP_MANAGER_PHONE: str = "+10000000000"

    SENDGRID_API_KEY: str = ""
    SENDGRID_FROM_EMAIL: str = "noreply@datawebify.com"
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""

    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""

    REDIS_URL: str = "redis://localhost:6379/0"
    

@lru_cache()
def get_settings() -> Settings:
    return Settings()