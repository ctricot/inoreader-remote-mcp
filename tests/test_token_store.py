import stat

from inoreader_remote_mcp.token_store import InoreaderTokens, TokenStore


def test_missing_file_is_no_tokens(tmp_path):
    assert TokenStore(tmp_path / "none.json").load() is None


def test_round_trip_and_permissions(tmp_path):
    store = TokenStore(tmp_path / "sub" / "tokens.json")
    tokens = InoreaderTokens("a", "r", 123.0, "read write")
    store.save(tokens)

    assert store.load() == tokens
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    # Nothing left behind by the atomic write.
    assert [p.name for p in store.path.parent.iterdir()] == ["tokens.json"]


def test_overwrite_tightens_loose_permissions(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.path.write_text("{}")
    store.path.chmod(0o644)
    store.save(InoreaderTokens("a", "r", 1.0))
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
