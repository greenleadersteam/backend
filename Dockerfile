# greenplan API image. pip builds the project directly via pyproject.toml's
# poetry-core backend (PEP 517) -- no Poetry needed inside the image, and
# poetry.lock is irrelevant here since pip does its own resolution against
# the version ranges already pinned in [project.dependencies].
#
# shapely/ezdxf/geopandas/pydantic-core all ship manylinux wheels for
# linux/amd64, so no compiler toolchain or system GEOS/GDAL packages are
# needed. If you deploy to linux/arm64 (e.g. an ARM cloud VM) and a
# dependency lacks an arm64 wheel, pip will fall back to a source build and
# this image will need `build-essential` added below.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY greenplan ./greenplan

RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 greenplan \
    && mkdir -p /data \
    && chown -R greenplan:greenplan /data
USER greenplan

ENV GREENPLAN_API_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8000

# Exactly one worker: JobManager's concurrency gate and ProjectStore's
# metadata cache are per-process in-memory state, not shared across
# processes. Running multiple uvicorn workers would give each one its own
# independent concurrency limit and its own stale-on-write metadata cache --
# scale by giving one worker more CPU/RAM, not by adding workers.
CMD ["uvicorn", "greenplan.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
