MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def test_health_is_public(http):
    response = http.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_mcp_requires_a_token_and_says_where_to_get_one(http):
    response = http.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE)
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert "Bearer" in challenge
    assert "http://localhost/.well-known/oauth-protected-resource/mcp" in challenge


def test_mcp_refuses_an_unknown_token(http):
    headers = {**MCP_HEADERS, "Authorization": "Bearer forged"}
    assert http.post("/mcp", headers=headers, json=INITIALIZE).status_code == 401


def test_mcp_answers_with_a_token(http, bearer):
    response = http.post("/mcp", headers={**MCP_HEADERS, **bearer}, json=INITIALIZE)
    assert response.status_code == 200
    assert '"name":"inoreader"' in response.text.replace(" ", "")


def test_unknown_host_is_refused(http, bearer):
    headers = {**MCP_HEADERS, **bearer, "Host": "evil.example"}
    assert http.post("/mcp", headers=headers, json=INITIALIZE).status_code == 421
