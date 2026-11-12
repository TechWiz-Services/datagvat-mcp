import pytest
from pydantic import ValidationError

from datagvat_mcp.config import Settings, get_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for name in list(os.environ):
        if name.startswith("DATAGVAT_"):
            monkeypatch.delenv(name)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_are_polite_and_safe():
    s = Settings()
    assert s.api_base == "https://www.data.gv.at/api/hub/search"
    # One request at a time with a 2 s pause: what the Federal Chancellery asks of automated clients.
    assert s.request_interval == 2.0
    hour, day = 3600, 86400
    assert {c: s.cache_ttl(c) for c in ("search", "dataset", "catalogue", "vocabulary", "preview")} == {
        "search": 6 * hour, "dataset": 7 * day, "catalogue": 7 * day, "vocabulary": 30 * day, "preview": hour}
    mb = 1024 * 1024
    assert s.cache_limits == {"search": 64 * mb, "dataset": 128 * mb, "catalogue": 16 * mb, "vocabulary": 32 * mb,
                              "preview": 64 * mb}
    assert s.cache_max_stale == 30 * day and s.cache_dir == "" and s.cache_enabled is True
    assert set(Settings(cache_enabled=False).cache_limits.values()) == {0}
    assert s.preview_max_bytes == 512 * 1024
    assert (s.host, s.port, s.path) == ("0.0.0.0", 8000, "/mcp")
    assert s.token is None and s.allowed_host_list == [] and s.stateless is False
    assert s.language == "de" and s.contact == ""


def test_values_come_from_prefixed_environment_variables(monkeypatch):
    env = {
        "DATAGVAT_AUTH_TOKEN": "s3cret", "DATAGVAT_PORT": "9000", "DATAGVAT_PATH": "datagvat",
        "DATAGVAT_ALLOWED_HOSTS": " mcp.example.org , localhost:* ,,", "DATAGVAT_STATELESS": "true",
        "DATAGVAT_LANGUAGE": " EN ", "DATAGVAT_REQUEST_INTERVAL": "0.5", "DATAGVAT_QUEUE_TIMEOUT": "20",
        "DATAGVAT_CONTACT": "ops@example.org", "DATAGVAT_CACHE_SEARCH_TTL": "0", "DATAGVAT_UNKNOWN_OPTION": "ignored",
        "DATAGVAT_CACHE_PREVIEW_MAX_MB": "0.5", "DATAGVAT_CACHE_DIR": "/data/cache",
        "PORT": "1234",  # unprefixed variables must not leak in
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = get_settings()
    assert s.token == "s3cret" and s.port == 9000 and s.path == "/datagvat"
    assert s.allowed_host_list == ["mcp.example.org", "localhost:*"]
    assert s.stateless is True and s.language == "en"
    assert (s.request_interval, s.queue_timeout, s.cache_ttl("search")) == (0.5, 20, 0)
    assert s.cache_limits["preview"] == 512 * 1024 and s.cache_dir == "/data/cache"
    assert s.contact == "ops@example.org"
    assert get_settings() is s  # read once, then cached


def test_empty_token_means_no_auth(monkeypatch):
    monkeypatch.setenv("DATAGVAT_AUTH_TOKEN", "")  # what `DATAGVAT_AUTH_TOKEN: ${DATAGVAT_AUTH_TOKEN:-}` yields
    assert Settings().token is None
    assert Settings(language="").language == "de"


def test_token_is_not_exposed_by_repr_or_dump():
    s = Settings(auth_token="s3cret-value")
    assert "s3cret-value" not in repr(s) and "s3cret-value" not in str(s.model_dump())
    assert s.token == "s3cret-value"


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_interval": -1}, {"request_interval": 61}, {"queue_timeout": 0}, {"port": 0}, {"port": 70000},
        {"request_timeout": 1}, {"preview_max_bytes": 100}, {"preview_max_bytes": 64 * 1024 * 1024},
        {"cache_search_ttl": -1}, {"cache_dataset_max_mb": -1}, {"cache_max_stale": -1}, {"stateless": "maybe"},
    ],
)
def test_out_of_range_values_are_rejected(overrides):
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_ris_is_blocked_unless_the_operator_opts_in(monkeypatch):
    assert Settings().preview_blocked_host_list == ["ris.bka.gv.at", "data.bka.gv.at"]
    monkeypatch.setenv("DATAGVAT_PREVIEW_BLOCKED_HOSTS", "")
    assert Settings().preview_blocked_host_list == []
    monkeypatch.setenv("DATAGVAT_PREVIEW_BLOCKED_HOSTS", " Example.org. , ,other.example ")
    assert Settings().preview_blocked_host_list == ["example.org", "other.example"]


async def test_default_services_block_ris_previews():
    from datagvat_mcp.preview import PreviewError
    from datagvat_mcp.server import make_services

    svc = make_services(Settings())
    try:
        with pytest.raises(PreviewError, match="disabled on this server"):
            await svc.preview.fetch("https://data.bka.gv.at/ris/api/v2.6/Bundesrecht")
    finally:
        await svc.client.close()


def test_log_level_is_normalised_and_checked():
    assert Settings().log_level == "INFO"
    assert Settings(log_level=" debug ").log_level == "DEBUG"
    with pytest.raises(ValidationError, match="DEBUG, INFO"):
        Settings(log_level="chatty")


def test_bad_environment_value_fails_at_startup(monkeypatch):
    monkeypatch.setenv("DATAGVAT_REQUEST_INTERVAL", "fast")
    with pytest.raises(ValidationError):
        get_settings()
