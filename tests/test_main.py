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
REQUIRED_ENV = {"DATAGVAT_AUTH_TOKEN": "t0ken-for-tests", "DATAGVAT_CONTACT": "ops@example.org"}


@pytest.fixture
def started(monkeypatch):
    """Run main() with uvicorn.run replaced; returns a function env -> captured uvicorn arguments.

    The two required settings are filled in unless `drop` names them."""
    for name in list(os.environ):
        if name.startswith("DATAGVAT_"):
            monkeypatch.delenv(name)
    levels = {name: logging.getLogger(name).level for name in NOISY}
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(app=app, **kwargs))

    def start(drop=(), **env):
        for k, v in {**{k: v for k, v in REQUIRED_ENV.items() if k not in drop}, **env}.items():
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


# ------------------------------------------------- required settings


@pytest.mark.parametrize(
    "drop,env,names",
    [
        (("DATAGVAT_AUTH_TOKEN",), {}, ["DATAGVAT_AUTH_TOKEN"]),
        (("DATAGVAT_CONTACT",), {}, ["DATAGVAT_CONTACT"]),
        (("DATAGVAT_AUTH_TOKEN", "DATAGVAT_CONTACT"), {}, ["DATAGVAT_AUTH_TOKEN", "DATAGVAT_CONTACT"]),
        ((), {"DATAGVAT_AUTH_TOKEN": ""}, ["DATAGVAT_AUTH_TOKEN"]),  # what an empty `.env` line yields
        ((), {"DATAGVAT_AUTH_TOKEN": "   "}, ["DATAGVAT_AUTH_TOKEN"]),
        ((), {"DATAGVAT_CONTACT": ""}, ["DATAGVAT_CONTACT"]),
        ((), {"DATAGVAT_CONTACT": " \t "}, ["DATAGVAT_CONTACT"]),
    ],
)
def test_refuses_to_start_without_the_required_settings(started, drop, env, names):
    with pytest.raises(SystemExit) as stop:
        started(drop=drop, **env)
    message = str(stop.value.code)
    assert "cannot start" in message
    assert all(name in message for name in names)
    others = {"DATAGVAT_AUTH_TOKEN", "DATAGVAT_CONTACT"} - set(names)
    assert not any(name in message for name in others)  # only what is actually missing is named
    assert "openssl rand -hex 32" in message or "DATAGVAT_AUTH_TOKEN" not in names  # tells how to get a token


def test_the_server_does_not_run_after_a_refusal(monkeypatch):
    for name in list(os.environ):
        if name.startswith("DATAGVAT_"):
            monkeypatch.delenv(name)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append(1))
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit):
            entry.main()
    finally:
        get_settings.cache_clear()
    assert calls == []


def test_the_token_is_never_printed_in_the_refusal(started):
    with pytest.raises(SystemExit) as stop:
        started(drop=("DATAGVAT_CONTACT",), DATAGVAT_AUTH_TOKEN="s3cret-value")
    assert "s3cret-value" not in str(stop.value.code)
