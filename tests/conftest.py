"""Shared fixtures. Zero network: every Inoreader call goes through respx."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from inoreader_remote_mcp.claude_oauth import Provider
from inoreader_remote_mcp.config import Settings
from inoreader_remote_mcp.inoreader import InoreaderClient
from inoreader_remote_mcp.server import create_app
from inoreader_remote_mcp.token_store import InoreaderTokens, TokenStore

NOW = 1_800_000_000.0
ADMIN_PASSWORD = "s3cret"


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        public_url="http://localhost",
        data_dir=tmp_path,
        inoreader_app_id="app-id",
        inoreader_app_key="app-key",
        admin_password=ADMIN_PASSWORD,
    )


@pytest.fixture
def store(settings) -> TokenStore:
    return TokenStore(settings.inoreader_tokens_path)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(settings, store, clock) -> InoreaderClient:
    return InoreaderClient(settings, store, clock=clock)


@pytest.fixture
def provider(settings, clock) -> Provider:
    return Provider(settings, clock=clock)


@pytest.fixture
def http(settings, client, provider):
    """The whole application, as Claude and the browser reach it."""
    app = create_app(settings, client, provider)
    with TestClient(app, base_url="http://localhost", follow_redirects=False) as c:
        yield c


@pytest.fixture
def bearer(provider) -> dict[str, str]:
    """A valid access token for `/mcp`, without going through the consent flow."""
    token = provider._issue("test-client", None)
    return {"Authorization": f"Bearer {token.access_token}"}


@pytest.fixture
def authorised(store) -> InoreaderTokens:
    tokens = InoreaderTokens(
        access_token="access-1",
        refresh_token="refresh-1",
        expires_at=NOW + 3600,
        scope="read write",
    )
    store.save(tokens)
    return tokens
