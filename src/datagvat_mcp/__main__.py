"""Run the server: python -m datagvat_mcp"""

import logging

import uvicorn

from datagvat_mcp.app import create_app
from datagvat_mcp.config import get_settings


def main() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Don't log every upstream URL / MCP request at INFO.
    for noisy in ("httpx", "mcp.server.lowlevel.server", "mcp.server.streamable_http"):
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
