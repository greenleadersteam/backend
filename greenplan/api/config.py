from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GREENPLAN_API_")

    data_dir: Path = Path("data")
    max_concurrent_jobs: int = Field(default=2, ge=1)
    max_upload_mb: int = Field(default=300, ge=1)
    # Comma-separated, not a JSON list: this is meant to be set from a single
    # plain env var (e.g. in docker-compose.yml's `environment:` block)
    # without needing shell-quoted JSON. `localhost`/`127.0.0.1` are distinct
    # origins to a browser even though they're the same machine, so both are
    # listed for each common frontend dev-server port.
    cors_origins: str = (
        "https://app.greenleaders.online,"
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:4200,http://127.0.0.1:4200,"
        "http://localhost:8080,http://127.0.0.1:8080"
    )

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
