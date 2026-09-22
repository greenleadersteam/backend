from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GREENPLAN_API_")

    data_dir: Path = Path("data")
    max_concurrent_jobs: int = Field(default=2, ge=1)
    max_upload_mb: int = Field(default=100, ge=1)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024
