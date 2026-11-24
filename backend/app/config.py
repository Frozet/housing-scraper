from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://avito:avito_local_only@localhost:55432/avito"
    request_delay_seconds: float = Field(default=5, ge=1)
    request_timeout_seconds: float = Field(default=30, ge=1, le=90)
    max_pages: int = Field(default=0, ge=0)
    max_listings: int = Field(default=0, ge=0)
    worker_enabled: bool = True
    collector_mode: Literal["browser", "http"] = "browser"
    browser_wait_seconds: int = Field(default=300, ge=30, le=1800)
    avito_proxy_url: SecretStr | None = None
    avito_proxy_required: bool = False
    map_overpass_url: str = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_tile_attribution: str = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'

    @field_validator("avito_proxy_url", mode="before")
    @classmethod
    def empty_proxy_is_unset(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


@lru_cache
def settings() -> Settings:
    return Settings()
