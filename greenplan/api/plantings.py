"""Versioned planting plans, stored under `processed/plantings/`:

    counter.yaml             project-wide manual-point id counter
    1/ metadata.yaml  planting.geojson  planting.dxf  explanation.json
    2/ metadata.yaml  planting.geojson  [planting.dxf  explanation.json]

Version 1 is the automatic layout (copied from processed/planting.* when
processing finishes, or on first access for projects processed before
versioning existed). Every saved edit creates a new version -- a full copy of
its base with the edit applied -- so history is linear and versions never
change once written, except for their export_status.

The version number is claimed by creating its folder (mkdir fails if it
already exists), and metadata.yaml is written last: a folder without it is an
unfinished save and is ignored. metadata.yaml is always read fresh from disk,
never cached, because the export worker process updates export_status.

Import-light on purpose (no shapely/pyproj): edits are applied to raw GeoJSON
dicts in the API process. The DXF/explanation export for versions 2+ runs in
a worker, see greenplan.api.jobs.run_version_export_job.
"""

from __future__ import annotations

import json
import math
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from greenplan.api._yamlio import atomic_write_yaml, read_yaml

PLANTINGS_DIRNAME = "plantings"
VERSION_METADATA_FILENAME = "metadata.yaml"
COUNTER_FILENAME = "counter.yaml"
GEOJSON_FILENAME = "planting.geojson"
DXF_FILENAME = "planting.dxf"
EXPLANATION_FILENAME = "explanation.json"
# Written by run_processing_job: what a version export needs to get from
# WGS84 back to the root drawing's local frame. Absent for projects processed
# before versioning existed -- their versions 2+ can't be exported.
EXPORT_CONTEXT_FILENAME = "export_context.yaml"

FIRST_VERSION_NAME = "Автоматическая посадка"
MANUAL_ID_PREFIX = "manual-"
# Edited coordinates may sit this far (degrees, ~1km) outside the project's bbox_user.
BBOX_MARGIN_DEG = 0.01

EXPORT_NONE = "none"
EXPORT_PENDING = "pending"
EXPORT_READY = "ready"
EXPORT_FAILED = "failed"


class ExportError(BaseModel):
    code: str = Field(description="`export_failed` or `transform_unavailable`.")
    message: str


class PlantingCounts(BaseModel):
    tree: int
    shrub: int
    total: int


class PlantingVersion(BaseModel):
    id: int = Field(description="Version number, 1-based. Version 1 is the automatic layout.")
    name: str
    kind: Literal["auto", "manual"] = Field(description="`auto` for version 1, `manual` for saved edits.")
    created_at: datetime
    based_on: int | None = Field(description="The version this one was edited from; `null` for version 1.")
    counts: PlantingCounts
    export_status: Literal["none", "pending", "ready", "failed"] = Field(
        description="State of this version's DXF/explanation files: `none` (not generated yet -- "
        "requesting either file starts generation), `pending` (being generated), `ready`, `failed`."
    )
    export_error: ExportError | None = Field(default=None, description="Set when `export_status` is `failed`.")


class PlantingAdd(BaseModel):
    client_id: str = Field(
        description="Any client-side id; echoed back in the response's `id_map` with the assigned id."
    )
    plant_type: Literal["tree", "shrub"]
    lon: float = Field(description="WGS84 longitude.")
    lat: float = Field(description="WGS84 latitude.")


class PlantingUpdate(BaseModel):
    id: str = Field(description="Id of an existing point in the base version.")
    lon: float | None = Field(default=None, description="New WGS84 longitude (give together with `lat`).")
    lat: float | None = Field(default=None, description="New WGS84 latitude (give together with `lon`).")
    plant_type: Literal["tree", "shrub"] | None = Field(default=None, description="New plant type.")


class PlantingEdit(BaseModel):
    """A batch of changes to one version, saved as a new version. Applied
    all-or-nothing: any invalid item rejects the whole edit with 422.
    """

    name: str | None = Field(default=None, description='Version name; default "Версия N".')
    add: list[PlantingAdd] = []
    update: list[PlantingUpdate] = []
    delete: list[str] = Field(default=[], description="Ids of points to remove.")


class EditValidationError(ValueError):
    """`errors` use FastAPI's request-validation shape ({loc, msg, type}), so
    the API returns them exactly like its own 422s. `type` is one of the
    EDIT_ERROR_* codes below.
    """

    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(e["msg"] for e in errors))
        self.errors = errors


EDIT_ERROR_EMPTY = "empty_edit"
EDIT_ERROR_DUPLICATE_CLIENT_ID = "duplicate_client_id"
EDIT_ERROR_DUPLICATE_ID = "duplicate_id"
EDIT_ERROR_UNKNOWN_ID = "unknown_id"
EDIT_ERROR_UPDATE_DELETE_CONFLICT = "update_delete_conflict"
EDIT_ERROR_NOT_FINITE = "coordinates_not_finite"
EDIT_ERROR_OUT_OF_BBOX = "outside_project_bbox"
EDIT_ERROR_LON_LAT_PAIR = "lon_lat_pair"
EDIT_ERROR_NOTHING_TO_UPDATE = "nothing_to_update"


class VersionNotFoundError(LookupError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def plantings_dir(processed_dir: Path) -> Path:
    return processed_dir / PLANTINGS_DIRNAME


def version_dir(processed_dir: Path, version_id: int) -> Path:
    return plantings_dir(processed_dir) / str(version_id)


def _counts(geojson: dict) -> PlantingCounts:
    types = [f["properties"]["plant_type"] for f in geojson["features"]]
    return PlantingCounts(tree=types.count("tree"), shrub=types.count("shrub"), total=len(types))


def _read_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict | list) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def write_version_metadata(vdir: Path, version: PlantingVersion) -> None:
    atomic_write_yaml(vdir / VERSION_METADATA_FILENAME, version.model_dump(mode="json"))


def read_version_metadata(vdir: Path) -> PlantingVersion | None:
    path = vdir / VERSION_METADATA_FILENAME
    if not path.is_file():
        return None
    return PlantingVersion.model_validate(read_yaml(path))


def set_export_status(vdir: Path, status: str, error: ExportError | None = None) -> None:
    version = read_version_metadata(vdir)
    if version is None:
        return
    write_version_metadata(vdir, version.model_copy(update={"export_status": status, "export_error": error}))


def create_first_version(processed_dir: Path) -> None:
    """Copy the automatic result into plantings/1/. Features and explanation
    entries written before `kind` existed are upgraded to kind "auto".
    """
    vdir = version_dir(processed_dir, 1)
    vdir.mkdir(parents=True, exist_ok=True)

    geojson = _read_json(processed_dir / GEOJSON_FILENAME)
    for feature in geojson["features"]:
        feature["properties"].setdefault("kind", "auto")
    _write_json(vdir / GEOJSON_FILENAME, geojson)

    explanation = _read_json(processed_dir / EXPLANATION_FILENAME)
    for entry in explanation:
        entry.setdefault("kind", "auto")
        entry.setdefault("moved", False)
    _write_json(vdir / EXPLANATION_FILENAME, explanation)

    shutil.copyfile(processed_dir / DXF_FILENAME, vdir / DXF_FILENAME)

    write_version_metadata(
        vdir,
        PlantingVersion(
            id=1,
            name=FIRST_VERSION_NAME,
            kind="auto",
            created_at=_now(),
            based_on=None,
            counts=_counts(geojson),
            export_status=EXPORT_READY,
        ),
    )


def reconcile_interrupted_exports(projects_root: Path) -> None:
    """Startup housekeeping: a 'pending' export was orphaned by a restart
    (its worker is gone). Reset it to 'none' so the next request retries it.
    """
    if not projects_root.is_dir():
        return
    for vdir in projects_root.glob(f"*/processed/{PLANTINGS_DIRNAME}/*"):
        if not vdir.is_dir():
            continue
        version = read_version_metadata(vdir)
        if version is not None and version.export_status == EXPORT_PENDING:
            set_export_status(vdir, EXPORT_NONE)


def _apply_edit(
    base: dict,
    edit: PlantingEdit,
    new_version_id: int,
    next_manual: int,
    bbox: tuple[float, float, float, float] | None,
) -> tuple[dict, dict[str, str], int]:
    """Returns (new geojson, client_id -> assigned id, next manual counter).
    Raises EditValidationError without applying anything.
    """
    errors: list[dict] = []
    features_by_id = {f["properties"]["id"]: f for f in base["features"]}

    def error(loc: tuple, error_type: str, msg: str) -> None:
        errors.append({"loc": ["body", *loc], "msg": msg, "type": error_type})

    def check_coords(loc: tuple, lon: float, lat: float) -> None:
        if not (math.isfinite(lon) and math.isfinite(lat)):
            error(loc, EDIT_ERROR_NOT_FINITE, "coordinates must be finite numbers")
            return
        if bbox is None:  # project without georeferencing: coordinates are in the drawing's frame
            return
        minx, miny, maxx, maxy = bbox
        if not (minx - BBOX_MARGIN_DEG <= lon <= maxx + BBOX_MARGIN_DEG
                and miny - BBOX_MARGIN_DEG <= lat <= maxy + BBOX_MARGIN_DEG):
            error(loc, EDIT_ERROR_OUT_OF_BBOX, f"point ({lon}, {lat}) is outside the project bbox")

    if not (edit.add or edit.update or edit.delete):
        error((), EDIT_ERROR_EMPTY, "edit is empty: add, update and delete are all empty")

    seen_client_ids: set[str] = set()
    for i, item in enumerate(edit.add):
        if item.client_id in seen_client_ids:
            error(("add", i, "client_id"), EDIT_ERROR_DUPLICATE_CLIENT_ID, f"duplicate client_id {item.client_id!r}")
        seen_client_ids.add(item.client_id)
        check_coords(("add", i), item.lon, item.lat)

    seen_update_ids: set[str] = set()
    for i, item in enumerate(edit.update):
        if item.id in seen_update_ids:
            error(("update", i, "id"), EDIT_ERROR_DUPLICATE_ID, f"duplicate id {item.id!r}")
        seen_update_ids.add(item.id)
        if item.id not in features_by_id:
            error(("update", i, "id"), EDIT_ERROR_UNKNOWN_ID, f"no point with id {item.id!r} in this version")
        if (item.lon is None) != (item.lat is None):
            error(("update", i), EDIT_ERROR_LON_LAT_PAIR, "lon and lat must be given together")
        elif item.lon is not None:
            check_coords(("update", i), item.lon, item.lat)
        if item.lon is None and item.lat is None and item.plant_type is None:
            error(("update", i), EDIT_ERROR_NOTHING_TO_UPDATE, "nothing to change: give lon/lat and/or plant_type")

    for i, point_id in enumerate(edit.delete):
        if point_id not in features_by_id:
            error(("delete", i), EDIT_ERROR_UNKNOWN_ID, f"no point with id {point_id!r} in this version")
        if point_id in seen_update_ids:
            error(("delete", i), EDIT_ERROR_UPDATE_DELETE_CONFLICT, f"id {point_id!r} is both updated and deleted")

    if errors:
        raise EditValidationError(errors)

    new = json.loads(json.dumps(base))  # deep copy
    new_by_id = {f["properties"]["id"]: f for f in new["features"]}
    for item in edit.update:
        feature = new_by_id[item.id]
        if item.lon is not None:
            feature["geometry"] = {"type": "Point", "coordinates": [item.lon, item.lat]}
        if item.plant_type is not None:
            feature["properties"]["plant_type"] = item.plant_type
    deleted = set(edit.delete)
    new["features"] = [f for f in new["features"] if f["properties"]["id"] not in deleted]

    id_map: dict[str, str] = {}
    for item in edit.add:
        point_id = f"{MANUAL_ID_PREFIX}{next_manual:05d}"
        next_manual += 1
        id_map[item.client_id] = point_id
        new["features"].append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [item.lon, item.lat]},
                "properties": {
                    "id": point_id,
                    "plant_type": item.plant_type,
                    "kind": "manual",
                    "rule_id": None,
                    "added_in_version": new_version_id,
                },
            }
        )
    return new, id_map, next_manual


class PlantingStore:
    """Version operations for one API process. The lock serializes version
    creation per project (together with mkdir claiming the folder); worker
    processes only ever touch export_status and export files of an existing
    version.
    """

    def __init__(self) -> None:
        self._locks: dict[Path, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, processed_dir: Path) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(processed_dir.resolve(), threading.Lock())

    def ensure_initialized(self, processed_dir: Path) -> None:
        """Projects processed before versioning existed get version 1 on first access."""
        if read_version_metadata(version_dir(processed_dir, 1)) is not None:
            return
        with self._lock(processed_dir):
            if read_version_metadata(version_dir(processed_dir, 1)) is None:
                create_first_version(processed_dir)

    def list_versions(self, processed_dir: Path) -> list[PlantingVersion]:
        self.ensure_initialized(processed_dir)
        versions = []
        for vdir in plantings_dir(processed_dir).iterdir():
            if vdir.is_dir() and vdir.name.isdigit():
                version = read_version_metadata(vdir)
                if version is not None:
                    versions.append(version)
        return sorted(versions, key=lambda v: v.id)

    def get_version(self, processed_dir: Path, version_id: int) -> PlantingVersion:
        self.ensure_initialized(processed_dir)
        version = read_version_metadata(version_dir(processed_dir, version_id))
        if version is None:
            raise VersionNotFoundError(version_id)
        return version

    def latest_version(self, processed_dir: Path) -> PlantingVersion:
        return self.list_versions(processed_dir)[-1]

    def read_geojson(self, processed_dir: Path, version_id: int) -> str:
        self.get_version(processed_dir, version_id)
        return (version_dir(processed_dir, version_id) / GEOJSON_FILENAME).read_text(encoding="utf-8")

    def create_edited_version(
        self,
        processed_dir: Path,
        base_id: int,
        edit: PlantingEdit,
        bbox: tuple[float, float, float, float] | None,
    ) -> tuple[PlantingVersion, dict[str, str]]:
        self.get_version(processed_dir, base_id)
        root = plantings_dir(processed_dir)
        with self._lock(processed_dir):
            base = _read_json(version_dir(processed_dir, base_id) / GEOJSON_FILENAME)
            counter_path = root / COUNTER_FILENAME
            next_manual = read_yaml(counter_path)["next_manual"] if counter_path.is_file() else 1

            # Counts unfinished folders too, so an interrupted save's number is never reused.
            new_id = max(int(p.name) for p in root.iterdir() if p.is_dir() and p.name.isdigit()) + 1
            new, id_map, next_manual = _apply_edit(base, edit, new_id, next_manual, bbox)

            vdir = root / str(new_id)
            vdir.mkdir()  # exclusive: fails rather than overwrite an existing version
            atomic_write_yaml(counter_path, {"next_manual": next_manual})
            _write_json(vdir / GEOJSON_FILENAME, new)
            version = PlantingVersion(
                id=new_id,
                name=edit.name or f"Версия {new_id}",
                kind="manual",
                created_at=_now(),
                based_on=base_id,
                counts=_counts(new),
                export_status=EXPORT_NONE,
            )
            write_version_metadata(vdir, version)
        return version, id_map
