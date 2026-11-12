"""HTTP layer: /health, bearer auth, Host allowlist, and real Streamable HTTP sessions end to end."""

import json
import logging

import httpx
import pytest
from starlette.testclient import TestClient

from conftest import DATASET, make_settings
from datagvat_mcp import server
from datagvat_mcp.app import BearerTokenMiddleware, create_app
from datagvat_mcp.client import DataGvAtClient
from datagvat_mcp.preview import PreviewFetcher
from datagvat_mcp.throttle import HostThrottles

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def client_factory(fake_api):
    """TestClient for the real ASGI app; data.gv.at is replaced by the fake API."""

    def make(**overrides):
        settings = make_settings(**overrides)
        app = create_app(settings)
        # Built here, used on the app's own event loop: nothing in it is bound to a loop yet.
        server.set_services(server.Services(
            settings=settings,
            client=DataGvAtClient(settings, transport=httpx.MockTransport(fake_api)),
            preview=PreviewFetcher(max_bytes=1000, timeout=5, throttles=HostThrottles(0, 5),
                                   transport_factory=lambda: httpx.MockTransport(lambda r: httpx.Response(404))),
        ))
        return TestClient(app, base_url="http://testserver")

    yield make
    server.set_services(None)


def messages(response) -> list[dict]:
    """JSON-RPC messages of a Streamable HTTP response (SSE stream or plain JSON)."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    return [response.json()]


class Session:
    """A minimal MCP client speaking Streamable HTTP through the TestClient."""

    def __init__(self, http: TestClient, token: str | None = None, initialize: bool = True, path: str = "/mcp"):
        self.http, self.path, self.next_id = http, path, 1
        self.headers = dict(HEADERS)
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        if initialize:
            r = http.post(path, json=INIT, headers=self.headers)
            assert r.status_code == 200, r.text
            self.server_info = messages(r)[0]["result"]
            self.headers["mcp-session-id"] = r.headers["mcp-session-id"]
            self.headers["mcp-protocol-version"] = self.server_info["protocolVersion"]
            assert http.post(path, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                             headers=self.headers).status_code == 202

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        r = self.http.post(self.path, headers=self.headers,
                           json={"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        assert r.status_code == 200, r.text
        (reply,) = [m for m in messages(r) if m.get("id") == self.next_id]
        return reply

    def call(self, tool: str, arguments: dict | None = None) -> dict:
        return self.request("tools/call", {"name": tool, "arguments": arguments or {}})["result"]


# ----------------------------------------------------------------- health, auth


def test_health_is_public(client_factory):
    with client_factory(auth_token="s3cret") as c:
        r = c.get("/health")
        assert (r.status_code, r.text) == (200, "ok")
        assert c.get("/health?probe=1").status_code == 200
        assert c.head("/health").status_code == 200
        assert c.post("/health").status_code in (404, 405)


def test_token_required(client_factory):
    with client_factory(auth_token="s3cret") as c:
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        bad = c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": "Bearer wrong"})
        assert bad.status_code == 401 and bad.headers["www-authenticate"] == "Bearer"
        assert "s3cret" not in bad.text
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": "Basic s3cret"}).status_code == 401
        good = c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": "Bearer s3cret"})
        assert good.status_code == 200
        assert "datagvat" in good.text and "mcp-session-id" in good.headers


@pytest.mark.parametrize("header", ["Bearer s3cre", "Bearer s3cret2", "Bearer S3CRET", "Bearer", "Bearer ", "s3cret",
                                    "Token s3cret", "Bearer s3cret extra", "Bearer s3cret,s3cret", ""])
def test_wrong_credentials_are_rejected(client_factory, header):
    with client_factory(auth_token="s3cret") as c:
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": header}).status_code == 401


@pytest.mark.parametrize("header", ["Bearer s3cret", "bearer s3cret", "BEARER s3cret", "Bearer   s3cret  "])
def test_accepted_spellings_of_the_authorization_header(client_factory, header):
    with client_factory(auth_token="s3cret") as c:
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": header}).status_code == 200


def test_every_method_and_path_needs_the_token(client_factory):
    with client_factory(auth_token="s3cret") as c:
        for method in ("GET", "POST", "DELETE", "PUT", "OPTIONS", "HEAD"):
            assert c.request(method, "/mcp", headers=HEADERS).status_code == 401, method
        for path in ("/", "/mcp/", "/health/", "/healthz", "/HEALTH", "/nope", "/mcp?token=s3cret",
                     "/mcp?access_token=s3cret", "/mcp?authorization=Bearer%20s3cret"):
            assert c.post(path, json=INIT, headers=HEADERS).status_code == 401, path


async def _run_middleware(scope) -> tuple[bool, list[dict]]:
    """Call the middleware directly with a raw ASGI scope; returns (inner app reached, messages sent)."""
    reached, sent = False, []

    async def inner(scope, receive, send):
        nonlocal reached
        reached = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await BearerTokenMiddleware(inner, "s3cret")(scope, receive, send)
    return reached, sent


@pytest.mark.parametrize("path", ["/health/../mcp", "//health", "/health/", "/health%2F..%2Fmcp", "/mcp", ""])
async def test_only_the_exact_health_path_bypasses_auth(path):
    # Raw paths as an ASGI server would pass them on (no client-side normalisation).
    reached, sent = await _run_middleware({"type": "http", "method": "GET", "path": path, "headers": []})
    assert not reached and sent[0]["status"] == 401


async def test_middleware_scopes():
    reached, _ = await _run_middleware({"type": "http", "method": "GET", "path": "/health", "headers": []})
    assert reached
    reached, _ = await _run_middleware({"type": "lifespan"})
    assert reached  # startup/shutdown events are not client requests
    reached, sent = await _run_middleware({"type": "websocket", "path": "/mcp", "headers": []})
    assert not reached and sent == [{"type": "websocket.close", "code": 1008}]
    reached, _ = await _run_middleware(
        {"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"authorization", b"Bearer s3cret")]})
    assert reached
    reached, _ = await _run_middleware(
        {"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"authorization", b"Bearer \xff\xfe")]})
    assert not reached


def test_open_when_no_token(client_factory, caplog):
    with caplog.at_level(logging.WARNING, logger="datagvat_mcp.app"):
        with client_factory() as c:
            r = c.post("/mcp", json=INIT, headers=HEADERS)
            assert r.status_code == 200 and '"serverInfo"' in r.text
    assert "DATAGVAT_AUTH_TOKEN is not set" in caplog.text


def test_no_warning_when_a_token_is_set(client_factory, caplog):
    with caplog.at_level(logging.WARNING, logger="datagvat_mcp.app"):
        with client_factory(auth_token="s3cret"):
            pass
    assert "DATAGVAT_AUTH_TOKEN" not in caplog.text and "s3cret" not in caplog.text


def test_host_allowlist(client_factory):
    with client_factory(allowed_hosts="mcp.example.org, localhost:*") as c:
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Host": "evil.example"}).status_code in (400, 421)
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Host": "mcp.example.org"}).status_code == 200
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Host": "localhost:8000"}).status_code == 200
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Host": "mcp.example.org:8000"}).status_code in (400, 421)
        # A page on another origin (DNS rebinding / drive-by request from a browser) is refused.
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Host": "mcp.example.org",
                                                  "Origin": "https://evil.example"}).status_code in (400, 403)
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Host": "mcp.example.org",
                                                  "Origin": "https://mcp.example.org"}).status_code == 200
        assert c.get("/health", headers={"Host": "evil.example"}).status_code == 200  # health stays reachable


def test_custom_path(client_factory):
    with client_factory(path="/datagvat") as c:
        assert c.post("/datagvat", json=INIT, headers=HEADERS).status_code == 200
        assert c.post("/mcp", json=INIT, headers=HEADERS).status_code == 404


# ------------------------------------------------------ sessions, end to end


def test_full_session_over_http(client_factory, fake_api):
    with client_factory(auth_token="s3cret") as c:
        s = Session(c, token="s3cret")
        assert s.server_info["serverInfo"]["name"] == "datagvat"
        assert "data.gv.at" in s.server_info["instructions"] and "CC BY 4.0" in s.server_info["instructions"]

        tools = s.request("tools/list")["result"]["tools"]
        assert len(tools) == 14 and all(t["annotations"]["readOnlyHint"] for t in tools)

        found = s.call("search_datasets", {"query": "wien", "limit": 2})
        assert found["isError"] is False and found["structuredContent"]["count"] == 300
        assert json.loads(found["content"][0]["text"])["results"][0]["id"] == DATASET["id"]

        bad = s.call("get_dataset", {"dataset_id": "../../etc/passwd"})
        assert bad["isError"] is True and "Invalid dataset id" in bad["content"][0]["text"]

        unknown = s.call("drop_all_datasets")
        assert unknown["isError"] is True

        res = s.request("resources/read", {"uri": "datagvat://themes"})["result"]
        assert "OP_DATPRO" in res["contents"][0]["text"]
    assert len(fake_api.calls("/search")) == 1
    assert fake_api.requests[0].headers["user-agent"].startswith("datagvat-mcp/")


def test_session_rules(client_factory):
    with client_factory() as c:
        s = Session(c)
        list_tools = {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}
        no_session = {k: v for k, v in s.headers.items() if k != "mcp-session-id"}
        assert c.post("/mcp", json=list_tools, headers=no_session).status_code == 400
        unknown = {**s.headers, "mcp-session-id": "0" * 32}
        assert c.post("/mcp", json=list_tools, headers=unknown).status_code in (400, 404)
        assert c.post("/mcp", content=b"{not json", headers=s.headers).status_code == 400
        assert c.post("/mcp", json=list_tools, headers={**s.headers, "Accept": "text/html"}).status_code == 406
        assert c.post("/mcp", json=list_tools, headers=s.headers).status_code == 200
        # Ending the session makes its id unusable.
        assert c.delete("/mcp", headers=s.headers).status_code == 200
        assert c.post("/mcp", json=list_tools, headers=s.headers).status_code in (400, 404)


def test_two_sessions_share_cache_and_rate_limit(client_factory, fake_api):
    with client_factory() as c:
        a, b = Session(c), Session(c)
        assert a.headers["mcp-session-id"] != b.headers["mcp-session-id"]
        first = a.call("get_dataset", {"dataset_id": DATASET["id"]})["structuredContent"]
        second = b.call("get_dataset", {"dataset_id": DATASET["id"]})["structuredContent"]
        note = second.pop("cache")  # the second agent is told its answer is a saved copy
        assert first == second and note["stale"] is False and "cache" not in first
    assert len(fake_api.requests) == 1  # one upstream request served both agents


def test_stateless_mode_needs_no_session(client_factory, fake_api):
    with client_factory(stateless=True) as c:
        s = Session(c, initialize=False)
        tools = s.request("tools/list")["result"]["tools"]
        assert len(tools) == 14
        r = s.call("semantic_search_datasets", {"natural_query": "Luftgüte Linz"})
        # No session means no way to ask the client's model: plain search is used.
        assert r["isError"] is False and r["structuredContent"]["interpreted_as"]["method"] == "plain_text"
        init = c.post("/mcp", json=INIT, headers=HEADERS)
        assert init.status_code == 200 and "mcp-session-id" not in init.headers


def test_services_are_closed_on_shutdown(client_factory):
    with client_factory() as c:
        Session(c).call("list_vocabularies")
        svc = server.get_services()
    assert server._services is None and svc.client._http.is_closed
