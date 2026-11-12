"""Run the server: python -m datagvat_mcp"""

import logging

import uvicorn

from datagvat_mcp.app import create_app
from datagvat_mcp.config import get_settings


# Loggers that would print every upstream URL, every MCP request or whole response bodies.
# Upstream requests are logged by datagvat_mcp.client at DEBUG instead (path, status, duration).
NOISY_LOGGERS = ("httpx", "httpcore", "mcp.server.lowlevel.server", "mcp.server.streamable_http", "sse_starlette.sse")


def main() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        ws="none",  # MCP uses plain HTTP + SSE
        server_header=False,
        log_level=settings.log_level.lower(),
        # X-Forwarded-* are trusted from the IPs in FORWARDED_ALLOW_IPS (uvicorn default 127.0.0.1)
    )


if __name__ == "__main__":
    main()
