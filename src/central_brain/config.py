from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Principal(BaseModel):
    workspace_id: UUID
    actor_id: UUID
    roles: set[str] = Field(default_factory=set)
    sensitivities: list[str] = Field(default_factory=lambda: ["public", "internal"])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_url: str
    environment: Literal["local", "production"] = "local"
    public_url: str = "http://127.0.0.1:8080"
    session_secret: str = Field(min_length=32)
    central_brain_principals_json: str = "{}"
    oauth_principals_json: str = "{}"
    oauth_issuer: str = ""
    oauth_client_ids: list[str] = Field(default_factory=list)
    oauth_web_client_id: str = ""
    oauth_web_client_secret: str = ""
    oauth_scope_prefix: str = "central-brain"
    max_search_results: int = Field(default=20, ge=1, le=100)
    max_context_chars: int = Field(default=16000, ge=1000, le=100000)
    requests_per_minute: int = Field(default=60, ge=1, le=600)
    log_level: str = "info"

    @model_validator(mode="after")
    def validate_deployment(self):
        self.public_url = self.public_url.rstrip("/")
        url = urlsplit(self.public_url)
        if url.query or url.fragment or url.path or not url.hostname:
            raise ValueError("public_url must be an origin without a path")
        if self.environment == "production":
            if url.scheme != "https" or not self.oauth_issuer.startswith("https://"):
                raise ValueError("Production requires HTTPS and an OAuth issuer")
            if not self.oauth_web_client_id or not self.oauth_client_ids:
                raise ValueError("Production requires registered OAuth clients")
            if self.central_brain_principals_json != "{}":
                raise ValueError("Static bearer tokens are local-development only")
        elif url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Local mode must use a loopback public_url")
        return self

    def principals(self) -> dict[str, Principal]:
        return TypeAdapter(dict[str, Principal]).validate_json(self.central_brain_principals_json)

    def oauth_principals(self) -> dict[str, Principal]:
        return TypeAdapter(dict[str, Principal]).validate_json(self.oauth_principals_json)


@lru_cache
def get_settings() -> Settings:
    return Settings()
