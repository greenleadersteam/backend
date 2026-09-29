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

    # Point-based DXF georeferencing (greenplan.georeference) -- mandatory for
    # every API-created project, see api/jobs.py's STAGE_GEOREFERENCING.
    geobridge_base_url: str = "https://geobridge.ru/maps/pp/api"
    geobridge_timeout_s: float = Field(default=10.0, gt=0)
    georeference_min_points: int = Field(default=2, ge=2)
    georeference_residual_threshold_m: float = Field(default=3.0, gt=0)
    # UTM zone 37N -- covers Moscow, the only pilot-object scope today; not
    # auto-selected per project, see CLAUDE.md.
    georeference_utm_epsg: str = "EPSG:32637"

    # Shared Overture cache (`greenplan overture fetch --out-dir ...`); None
    # means data_dir/overture_cache. A missing/empty cache just skips fusion.
    overture_cache_dir: Path | None = None

    @property
    def resolved_overture_cache_dir(self) -> Path:
        return self.overture_cache_dir or self.data_dir / "overture_cache"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
