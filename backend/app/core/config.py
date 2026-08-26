"""Application configuration, loaded from environment variables / `.env`.

Every value below has a safe development default so that `docker compose up`
works with zero configuration. Copy `.env.example` to `.env` and override the
secrets before any non-local deployment — never commit a real `.env`.

Fail-fast: outside `APP_ENV=development` the app refuses to start with the
placeholder dev secrets (see `_enforce_production_secrets`).

See: docs/ARCHITECTURE.md §9 (environment driven by `.env`).
"""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Env var names map to field names (case-insensitive)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application ---------------------------------------------------
    app_name: str = "Nightingale API"
    app_env: str = "development"  # APP_ENV
    app_port: int = 8000  # APP_PORT

    # --- Database ------------------------------------------------------
    # The API connects as the restricted `app_nightingale` login role
    # (Pattern A: SET ROLE per request — see app/db/session.py), never the
    # bootstrap superuser. `app_db_user` / `app_db_password` document the
    # credentials that back `database_url` and are the target of the
    # production fail-fast check below.
    database_url: str = (
        "postgresql+psycopg://app_nightingale:dev_app_password_change_me"
        "@db:5432/nightingale"
    )  # DATABASE_URL
    app_db_user: str = "app_nightingale"  # APP_DB_USER
    app_db_password: str = "dev_app_password_change_me"  # APP_DB_PASSWORD

    # --- Auth / JWT ----------------------------------------------------
    jwt_secret: str = "dev_secret_change_me_at_least_32_bytes"  # JWT_SECRET — override in .env
    jwt_algorithm: str = "HS256"  # JWT_ALGORITHM
    # Dev default is long (480 min) for demo convenience; docs/SECURITY.md T8
    # recommends a 15-minute access token for production.
    jwt_expire_minutes: int = 480  # JWT_EXPIRE_MINUTES

    # --- LLM gateway ---------------------------------------------------
    # LLM_MOCK=1 (default) routes every model call to the deterministic
    # mock-llm service so tests/demos never depend on a live model.
    llm_mock: bool = True  # LLM_MOCK
    llm_base_url: str = "http://mock-llm:5000"  # LLM_BASE_URL
    llm_api_key: str | None = None  # LLM_API_KEY — only needed when LLM_MOCK=0
    llm_model: str = "mock-llm"  # LLM_MODEL

    # --- CORS ----------------------------------------------------------
    cors_origins: list[str] = [
        "http://localhost:5173",  # Vite dev server (web service)
        "http://localhost:8000",  # API itself
    ]  # CORS_ORIGINS (JSON array)

    @model_validator(mode="after")
    def _enforce_production_secrets(self) -> "Settings":
        """Refuse to boot with placeholder dev secrets outside development."""
        if self.app_env == "development":
            return self
        placeholders = {
            "JWT_SECRET": self.jwt_secret in {"", "dev_secret_change_me_at_least_32_bytes"},
            "APP_DB_PASSWORD": self.app_db_password in {"", "dev_app_password_change_me"},
        }
        missing = [name for name, is_placeholder in placeholders.items() if is_placeholder]
        if missing:
            raise ValueError(
                f"APP_ENV={self.app_env!r} requires real secrets; replace placeholder(s): "
                + ", ".join(missing)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (constructed once per process)."""
    return Settings()
