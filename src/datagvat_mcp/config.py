"""Configuration from environment variables (prefix DATAGVAT_)."""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from datagvat_mcp.cache import CATEGORIES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DATAGVAT_", extra="ignore")

    # --- MCP HTTP server ---------------------------------------------------
    host: str = "0.0.0.0"
    port: int = Field(8000, ge=1, le=65535)
    path: str = "/mcp"
    auth_token: SecretStr | None = None
    # Comma-separated Host header allowlist (DNS-rebinding protection). Empty = off.
    allowed_hosts: str = ""
    stateless: bool = False
    log_level: str = "INFO"

    # --- data.gv.at API ----------------------------------------------------
    api_base: str = "https://www.data.gv.at/api/hub/search"
    language: str = "de"
    request_timeout: float = Field(30.0, ge=5, le=120)
    # Appended to the User-Agent so data.gv.at can reach you (e.g. an e-mail address).
    contact: str = ""

    # --- Politeness: outbound rate limiting and caching ------------------
    # One request at a time; this many seconds of pause after each completed request.
    request_interval: float = Field(2.0, ge=0, le=60)
    queue_timeout: float = Field(30.0, ge=1, le=600)

    # --- Cache -------------------------------------------------------------
    # Master switch. False: nothing is saved or served from a saved copy, whatever the settings below say.
    cache_enabled: bool = True
    # Directory for the cache file. Empty: keep the cache in memory only (lost on restart,
    # at most 16 MB per category).
    cache_dir: str = ""
    # Lifetime in seconds and size limit in MB per kind of answer. 0 switches a kind off.
    cache_search_ttl: int = Field(6 * 3600, ge=0)  # dataset searches: the portal harvests daily
    cache_search_max_mb: float = Field(64, ge=0, le=102400)
    cache_dataset_ttl: int = Field(7 * 86400, ge=0)  # one dataset's metadata
    cache_dataset_max_mb: float = Field(128, ge=0, le=102400)
    cache_catalogue_ttl: int = Field(7 * 86400, ge=0)  # catalogue details and catalogue lists
    cache_catalogue_max_mb: float = Field(16, ge=0, le=102400)
    cache_vocabulary_ttl: int = Field(30 * 86400, ge=0)  # controlled vocabularies and their terms
    cache_vocabulary_max_mb: float = Field(32, ge=0, le=102400)
    cache_preview_ttl: int = Field(3600, ge=0)  # the first part of a data file; files can change daily
    cache_preview_max_mb: float = Field(64, ge=0, le=102400)
    # How long past its lifetime an answer is kept to be served when the source cannot be reached.
    cache_max_stale: int = Field(30 * 86400, ge=0)

    # --- Previews of distribution files (third-party hosts) --------------
    preview_max_bytes: int = Field(512 * 1024, ge=4096, le=8 * 1024 * 1024)
    # Like request_interval, but per file host. Separate, because these are often small
    # servers of municipalities: speeding up the API must not speed up downloads from them.
    preview_request_interval: float = Field(2.0, ge=0, le=60)
    # Hosts (and their subdomains) the preview tools never download from, comma-separated.
    # Default: the federal legal information system RIS. It has its own strict access rules,
    # and legal texts are not what this server is for. Set to an empty value to allow it.
    preview_blocked_hosts: str = "ris.bka.gv.at,data.bka.gv.at"

    @field_validator("auth_token", mode="before")
    @classmethod
    def _empty_token_is_none(cls, v):
        return None if v in ("", None) else v

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return v

    @field_validator("path")
    @classmethod
    def _path_starts_with_slash(cls, v: str) -> str:
        return v if v.startswith("/") else "/" + v

    @field_validator("language")
    @classmethod
    def _lang_lower(cls, v: str) -> str:
        return v.strip().lower() or "de"

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    def startup_problems(self) -> list[str]:
        """What stops the server from starting. Empty when the required settings are in place."""
        problems = []
        if not (self.token or "").strip():
            problems.append(
                "DATAGVAT_AUTH_TOKEN is not set. Every client must authenticate; "
                "generate a token with: openssl rand -hex 32"
            )
        if not self.contact.strip():
            problems.append(
                "DATAGVAT_CONTACT is not set. data.gv.at must be able to reach whoever runs this server; "
                "set it to an e-mail address or a URL"
            )
        return problems

    def cache_ttl(self, category: str) -> int:
        return getattr(self, f"cache_{category}_ttl")

    @property
    def cache_limits(self) -> dict[str, int]:
        """Size limit in bytes per cache category (all zero when caching is switched off)."""
        if not self.cache_enabled:
            return dict.fromkeys(CATEGORIES, 0)
        return {c: int(getattr(self, f"cache_{c}_max_mb") * 1024 * 1024) for c in CATEGORIES}

    @property
    def preview_blocked_host_list(self) -> list[str]:
        return [h.strip().lower().strip(".") for h in self.preview_blocked_hosts.split(",") if h.strip(" .")]

    @property
    def token(self) -> str | None:
        return self.auth_token.get_secret_value() if self.auth_token else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
