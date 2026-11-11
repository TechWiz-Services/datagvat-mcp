"""HTTP layer: /health, bearer auth, Host allowlist, real Streamable HTTP handshake."""

import pytest
from starlette.testclient import TestClient

from conftest import make_settings
from datagvat_mcp import server
from datagvat_mcp.app import create_app

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client_factory():
    # These tests never reach data.gv.at: only the MCP handshake is exercised.
    def make(**overrides):
        return TestClient(create_app(make_settings(**overrides)), base_url="http://testserver")

    yield make
    server.set_services(None)


def test_health_is_public(client_factory):
    with client_factory(auth_token="s3cret") as c:
        assert c.get("/health").text == "ok"


def test_token_required(client_factory):
    with client_factory(auth_token="s3cret") as c:
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        bad = c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": "Bearer wrong"})
        assert bad.status_code == 401 and bad.headers["www-authenticate"] == "Bearer"
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": "Basic s3cret"}).status_code == 401
        good = c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": "Bearer s3cret"})
        assert good.status_code == 200
        assert "datagvat" in good.text and "mcp-session-id" in good.headers


def test_open_when_no_token(client_factory):
    with client_factory() as c:
        r = c.post("/mcp", json=INIT, headers=HEADERS)
        assert r.status_code == 200 and '"serverInfo"' in r.text


def test_host_allowlist(client_factory):
    with client_factory(allowed_hosts="mcp.example.org") as c:
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Host": "evil.example"}).status_code in (400, 421)
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Host": "mcp.example.org"}).status_code == 200


def test_custom_path(client_factory):
    with client_factory(path="/datagvat") as c:
        assert c.post("/datagvat", json=INIT, headers=HEADERS).status_code == 200
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 404
