from functools import lru_cache

from pydantic import BaseModel, Field, TypeAdapter
from pydantic_settings import BaseSettings, SettingsConfigDict


class Principal(BaseModel):
    workspace_id: str
    actor_id: str
    roles: set[str] = Field(default_factory=set)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    central_brain_principals_json: str
    log_level: str = "info"
    max_search_results: int = Field(default=20, ge=1, le=100)

    def principals(self) -> dict[str, Principal]:
        return TypeAdapter(dict[str, Principal]).validate_json(
            self.central_brain_principals_json
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
