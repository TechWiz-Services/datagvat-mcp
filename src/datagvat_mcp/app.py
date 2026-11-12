"""ASGI application: MCP Streamable HTTP endpoint, /health, optional bearer auth."""

import hmac
import logging
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from datagvat_mcp.config import Settings, get_settings
from datagvat_mcp.server import build_server, close_services

HEALTH_PATH = "/health"


class BearerTokenMiddleware:
    """Require `Authorization: Bearer <token>` on every request except GET /health."""

    def __init__(self, app: ASGIApp, token: str | None):
        self.app = app
        self.token = token.encode() if token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Everything but the server's own lifespan events is a client connection (http, websocket).
        if self.token and scope["type"] != "lifespan" and scope.get("path") != HEALTH_PATH:
            auth = dict(scope.get("headers") or []).get(b"authorization", b"")
            scheme, _, supplied = auth.partition(b" ")
            if scheme.lower() != b"bearer" or not hmac.compare_digest(supplied.strip(), self.token):
                if scope["type"] == "http":
                    response = JSONResponse(
                        {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
                    )
                    await response(scope, receive, send)
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)


async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def create_app(settings: Settings | None = None) -> ASGIApp:
    settings = settings or get_settings()
    mcp = build_server(settings)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                await close_services()

    app = Starlette(
        routes=[Route(HEALTH_PATH, health, methods=["GET"]), Mount("/", app=mcp_app)],
        lifespan=lifespan,
    )
    if not settings.token:
        logging.getLogger(__name__).warning(
            "DATAGVAT_AUTH_TOKEN is not set: the MCP endpoint accepts unauthenticated requests"
        )
    return BearerTokenMiddleware(app, settings.token)
