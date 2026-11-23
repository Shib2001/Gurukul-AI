from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_ENV: str = "development"
    AI_SERVICE_AUTH_KEY: SecretStr = SecretStr("")
    AI_PROVIDER: Literal["fake", "openrouter"] = "fake"
    OPENROUTER_API_KEY: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("OPENROUTER_API_KEY", "OPEN_ROUTER"),
    )
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    REDIS_URL: str = "redis://localhost:6379/1"
    AI_IDEMPOTENCY_TTL_SECONDS: int = Field(default=86_400, ge=60, le=604_800)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
