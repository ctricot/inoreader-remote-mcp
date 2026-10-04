"""Configuration, read from the environment (and from `.env` in development).

Secrets never have a default: a missing `INOREADER_APP_KEY` must fail where it is used,
not silently fall back to something that looks like a key.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The address Claude and the browser reach, behind the reverse proxy. It is the OAuth
    # issuer, the base of every redirect URI, and the host the transport security accepts.
    public_url: str = "http://localhost:8000"

    # Where the token files live: a mounted volume in production.
    data_dir: Path = Path("./data")

    inoreader_app_id: str | None = None
    inoreader_app_key: SecretStr | None = None

    # The one secret that opens the admin routes (Inoreader authorisation and the consent
    # screen for Claude). Unset, those routes refuse everyone.
    admin_password: SecretStr | None = None

    log_level: str = "INFO"

    @field_validator("inoreader_app_id", "inoreader_app_key", "admin_password", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `ADMIN_PASSWORD=` left empty in `.env` arrives as "", and an empty password would
        # match an empty one typed in: it must mean « not configured », i.e. closed.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def public_host(self) -> str:
        return self.public_url.split("://", 1)[-1].rstrip("/")

    @property
    def inoreader_tokens_path(self) -> Path:
        return self.data_dir / "inoreader_tokens.json"

    @property
    def inoreader_redirect_uri(self) -> str:
        return self.url("/oauth/inoreader/callback")

    def url(self, path: str) -> str:
        return f"{self.public_url.rstrip('/')}{path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
