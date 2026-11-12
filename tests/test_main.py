"""The entry point: python -m datagvat_mcp"""

import logging
import os

import pytest
import uvicorn
from pydantic import ValidationError

from datagvat_mcp import __main__ as entry
from datagvat_mcp import server
from datagvat_mcp.config import get_settings

NOISY = entry.NOISY_LOGGERS


@pytest.fixture
def started(monkeypatch):
    """Run main() with uvicorn.run replaced; returns a function env -> captured uvicorn arguments."""
    for name in list(os.environ):
        if name.startswith("DATAGVAT_"):
            monkeypatch.delenv(name)
    levels = {name: logging.getLogger(name).level for name in NOISY}
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(app=app, **kwargs))

    def start(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        get_settings.cache_clear()
        entry.main()
        return captured

    yield start
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    get_settings.cache_clear()
    server.set_services(None)


def test_defaults(started):
    args = started()
    assert (args["host"], args["port"]) == ("0.0.0.0", 8000)
    assert args["ws"] == "none" and args["server_header"] is False and args["log_level"] == "info"
    assert callable(args["app"])
    # Upstream URLs and every MCP request are not logged at INFO.
    assert all(logging.getLogger(name).level == logging.WARNING for name in NOISY)


def test_environment_is_applied(started):
    args = started(DATAGVAT_HOST="127.0.0.1", DATAGVAT_PORT="9123", DATAGVAT_LOG_LEVEL="debug",
                   DATAGVAT_AUTH_TOKEN="s3cret")
    assert (args["host"], args["port"], args["log_level"]) == ("127.0.0.1", 9123, "debug")
    assert "s3cret" not in repr(args)
    # Even at DEBUG these stay quiet: they would dump whole response bodies into the log.
    assert {"sse_starlette.sse", "httpx"} <= set(NOISY)
    assert all(logging.getLogger(name).level == logging.WARNING for name in NOISY)


def test_invalid_configuration_stops_the_start(started):
    with pytest.raises(ValidationError, match="DEBUG, INFO"):
        started(DATAGVAT_LOG_LEVEL="chatty")  # uvicorn would otherwise die later with a bare KeyError
