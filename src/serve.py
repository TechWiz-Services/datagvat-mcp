"""HTTP entrypoint: serves the upstream FastMCP server over Streamable HTTP.

Environment:
  MCP_HOST        bind address            (default 0.0.0.0)
  MCP_PORT        port                    (default 8000)
  MCP_PATH        MCP endpoint path       (default /mcp)
  MCP_AUTH_TOKEN  if set, clients must send "Authorization: Bearer <token>"
  MCP_STATELESS   "true" = stateless HTTP (no sessions; disables sampling)
"""

import hmac
import os

import uvicorn
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from app.server import mcp

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8000"))
PATH = os.environ.get("MCP_PATH", "/mcp")
TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").encode() or None
STATELESS = os.environ.get("MCP_STATELESS", "false").lower() in ("1", "true", "yes")
HEALTH_PATH = "/health"


class BearerTokenMiddleware:
    """Reject requests without the configured bearer token (except /health)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if TOKEN and scope["type"] == "http" and scope["path"] != HEALTH_PATH:
            auth = dict(scope["headers"]).get(b"authorization", b"")
            scheme, _, supplied = auth.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(supplied.strip(), TOKEN):
                response = JSONResponse(
                    {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


@mcp.custom_route(HEALTH_PATH, methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


app = mcp.http_app(
    path=PATH,
    transport="http",
    stateless_http=STATELESS,
    middleware=[Middleware(BearerTokenMiddleware)],
)

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        ws="none",  # MCP uses plain HTTP + SSE; no websocket endpoint needed
        server_header=False,  # proxy headers: uvicorn honours FORWARDED_ALLOW_IPS from env
        log_level=os.environ.get("AUSTRIA_MCP_LOG_LEVEL", "info").lower(),
    )
