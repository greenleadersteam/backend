"""Per-project reads from the shared local Overture cache (see client.py for
how it's populated). A bbox read is cheap -- the cache's small row groups
let DuckDB skip most of a citywide file via row-group bbox stats (~0.05s for
a street-sized bbox against all 3M Moscow buildings).
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import duckdb
import shapely
from shapely.geometry.base import BaseGeometry

from greenplan.overture.cache import latest_fetch_per_type


def resolve_cache_file(cache_dir: Path, name: str, overture_type: str) -> tuple[Path, str] | None:
    """(parquet path, release) of the type's latest successful fetch, or None.

    The manifest's own `file` entry is relative to wherever `overture fetch`
    ran from, so only its filename is trusted and re-anchored to cache_dir.
    """
    entry = latest_fetch_per_type(cache_dir, name).get(overture_type)
    if entry is None:
        return None
    path = cache_dir / Path(entry["file"]).name
    if not path.is_file():
        return None
    return path, entry["release"]


def _sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def read_bbox(
    path: Path,
    bbox: tuple[float, float, float, float],
    columns: Sequence[str],
    where: str | None = None,
) -> list[tuple[dict, BaseGeometry]]:
    """Rows of a cached Overture GeoParquet file intersecting a WGS84 bbox,
    as ({column alias: value}, WGS84 shapely geometry) pairs.

    `columns` are DuckDB select expressions, optionally aliased
    ("names.primary AS name"); `where` is an extra SQL filter. Both are
    trusted, code-supplied SQL -- never pass user input here.
    """
    minx, miny, maxx, maxy = bbox
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    select = ", ".join(columns)
    query = (
        f"SELECT {select}, ST_AsWKB(geometry) AS __wkb FROM read_parquet({_sql_str(str(path))}) "
        f"WHERE bbox.xmin < {maxx} AND bbox.xmax > {minx} AND bbox.ymin < {maxy} AND bbox.ymax > {miny}"
    )
    if where:
        query += f" AND ({where})"
    cursor = con.execute(query)
    names = [d[0] for d in cursor.description]
    rows = []
    for row in cursor.fetchall():
        record = dict(zip(names, row))
        wkb = record.pop("__wkb")
        rows.append((record, shapely.from_wkb(bytes(wkb))))
    return rows
