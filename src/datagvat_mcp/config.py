"""Configuration from environment variables (prefix DATAGVAT_)."""

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    rate_limit: float = Field(1.0, gt=0, le=10, description="sustained requests per second")
    rate_burst: int = Field(3, ge=1, le=20)
    max_concurrency: int = Field(1, ge=1, le=4)
    queue_timeout: float = Field(60.0, ge=1, le=600)
    cache_ttl: int = Field(3600, ge=0, description="seconds; search results")
    cache_ttl_static: int = Field(86400, ge=0, description="seconds; datasets, catalogues, vocabularies")
    cache_max_entries: int = Field(2000, ge=0)

    # --- Previews of distribution files (third-party hosts) --------------
    preview_max_bytes: int = Field(512 * 1024, ge=4096, le=8 * 1024 * 1024)
    preview_cache_entries: int = Field(32, ge=0)

    @field_validator("auth_token", mode="before")
    @classmethod
    def _empty_token_is_none(cls, v):
        return None if v in ("", None) else v

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

    @property
    def token(self) -> str | None:
        return self.auth_token.get_secret_value() if self.auth_token else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
