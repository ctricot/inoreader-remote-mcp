from inoreader_remote_mcp.config import Settings


def test_blank_secrets_mean_unset(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setenv("INOREADER_APP_ID", " ")
    monkeypatch.setenv("INOREADER_APP_KEY", "")
    settings = Settings(_env_file=None)
    assert settings.admin_password is None
    assert settings.inoreader_app_id is None
    assert settings.inoreader_app_key is None


def test_empty_admin_password_keeps_routes_closed(http, settings):
    settings.admin_password = Settings(_env_file=None, admin_password="").admin_password
    assert http.get("/oauth/inoreader/start", auth=("owner", "")).status_code == 503
