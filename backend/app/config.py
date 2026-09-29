"""
Central settings. Everything privileged is read from the environment —
nothing here is ever safe to hardcode or ship to the frontend.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    ENV: str = "development"
    APP_SECRET_KEY: str  # used for session signing — required, no default
    FRONTEND_URL: str = "http://localhost:3000"

    # Database
    DATABASE_URL: str  # postgresql+psycopg://user:pass@host:5432/dbname

    # Redis (rate limiting, session store, background job queue)
    REDIS_URL: str = "redis://localhost:6379/0"

    # Retell — server-side only, NEVER exposed to frontend
    RETELL_API_KEY: str = ""
    RETELL_API_BASE_URL: str = "https://api.retellai.com"
    RETELL_WEBHOOK_MAX_SKEW_SECONDS: int = 300  # 5 minutes, per Retell's own replay window

    # Website-import structured extraction (server-side only — not the same
    # thing as "Claude powers the receptionist"; this is a one-off text
    # structuring call during onboarding, unrelated to Retell/call-time).
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_EXTRACTION_MODEL: str = "claude-sonnet-4-5"
    GROQ_API_KEY: str = ""
    GROQ_EXTRACTION_MODEL: str = "llama-3.3-70b-versatile"

    # Google Calendar OAuth
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/integrations/google/callback"

    # Token encryption (Fernet key) for integration access/refresh tokens at rest
    TOKEN_ENCRYPTION_KEY: str = ""

    # Session cookie
    SESSION_COOKIE_NAME: str = "atla_session"
    SESSION_TTL_SECONDS: int = 60 * 60 * 24 * 14  # 14 days

    # Rate limiting defaults (overridable per-org later via DB, these are floor defaults)
    LOGIN_ATTEMPTS_PER_15MIN: int = 8
    SIGNUP_ATTEMPTS_PER_HOUR_PER_IP: int = 5
    WEBSITE_IMPORTS_PER_DAY_PER_ORG: int = 20
    TEST_CALLS_PER_DAY_PER_ORG: int = 30

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
