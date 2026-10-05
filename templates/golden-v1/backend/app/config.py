"""Application settings, loaded from environment only (TEMPLATE_GUIDE.md rule 1)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    jwt_secret: str = "change-me-in-production-use-a-random-32-plus-byte-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = Field(default=60 * 24, ge=1)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])


@lru_cache
def get_settings() -> Settings:
    """Return the cached, process-wide `Settings` instance."""
    return Settings()
