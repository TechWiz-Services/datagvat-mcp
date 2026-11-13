"""Run the server: python -m datagvat_mcp"""

import logging
import sys

import uvicorn
from pydantic import ValidationError

from datagvat_mcp.app import create_app
from datagvat_mcp.config import get_settings


# Loggers that would print every upstream URL, every MCP request or whole response bodies.
# Upstream requests are logged by datagvat_mcp.client at DEBUG instead (path, status, duration).
NOISY_LOGGERS = ("httpx", "httpcore", "mcp.server.lowlevel.server", "mcp.server.streamable_http", "sse_starlette.sse")


def _describe(error: dict) -> str:
    """One invalid setting as 'DATAGVAT_NAME: reason (got value)'. The token is never shown."""
    field = str(error["loc"][0]) if error["loc"] else ""
    shown = "" if field == "auth_token" else f" (got {error['input']!r})"
    return f"DATAGVAT_{field.upper()}: {error['msg']}{shown}" if field else error["msg"]


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as e:
        sys.exit("datagvat-mcp cannot start, invalid settings:\n  - " + "\n  - ".join(_describe(x) for x in e.errors()))
    problems = settings.startup_problems()
    if problems:  # exits with a non-zero status and the reasons on stderr
        sys.exit("datagvat-mcp cannot start:\n  - " + "\n  - ".join(problems))
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
