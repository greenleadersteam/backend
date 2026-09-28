"""Fetch Overture Maps data for a bbox into GeoParquet files, for the shared
local cache (see CLAUDE.md's "Overture cache" design) -- not a live
per-project fetch. Even a slow fetch here is fine on an infrequent
(weekly/monthly) refresh schedule; it would not be fine per API request.

Deliberately minimal: one direct DuckDB SQL query per type, writing straight
to GeoParquet via COPY -- no STAC index, no pyarrow/geopandas round-trip
through Python. This replaced an earlier, more elaborate overturemaps-py-
based implementation (STAC-detect-and-fallback, a private _prepare_query()
reach-in, manual GeoDataFrame construction) after real testing showed the
extra machinery wasn't buying anything: a plain DuckDB COPY straight to
Parquet finished a full-Moscow `building` fetch in under 11 minutes,
reliably -- while the exact same data through a GeoDataFrame-conversion
path had stalled twice (S3 connections stuck in CLOSE-WAIT, no forward
progress for 30+ minutes), and going through GDAL's GPKG driver instead of
Parquet took over 2 hours for the same query. GDAL's GPKG writer (SQLite +
R-tree spatial index) is slow for millions of features regardless of
source; GeoParquet has no such index to build, which is most of why this
is fast.

Doesn't touch Overture's STAC index at all (confirmed broken 2026-09-26/27:
every row in stac.overturemaps.org's collections.parquet has a null
`collection` column, for every release checked) -- read_parquet's glob over
the whole theme/type partition is the only path, for every type.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
from overturemaps import core

# Roughly Moscow within MKAD plus a margin (WGS84 lon/lat). Does not cover
# the New Moscow (TiNAO) extension to the southwest -- pass a different
# bbox explicitly for that. This is the shared cache's default extent, a
# different concept from a per-project bbox_user (see
# greenplan.georeference.transform).
DEFAULT_MOSCOW_BBOX = (36.6537, 55.1591, 38.3675, 56.2012)

# The Overture types this project actually has a use for (buildings/roads/
# land-use/etc. -- see CLAUDE.md's "Overture cache" design). Deliberately
# narrower than core.get_all_overture_types(): address/place/division*/
# bathymetry/building_part aren't used by anything here. `land_cover` is a
# known type but excluded from the default set -- no current consumer, not
# worth the refresh-time cost yet; still fetchable via an explicit --types.
DEFAULT_CACHE_TYPES = ("building", "connector", "infrastructure", "land", "land_use", "segment", "water")

# Tuned for later small, per-project bbox reads against this cache (the
# whole point of building it): smaller row groups than Parquet's generic
# default let a reader skip more of the file via row-group bbox stats.
ROW_GROUP_SIZE = 50_000


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def fetch_type_to_parquet(
    overture_type: str,
    bbox: tuple[float, float, float, float],
    release: str,
    out_path: Path,
) -> int:
    """Fetch one Overture type for a bbox straight into a GeoParquet file at
    out_path. Returns the row count written; if 0, no file is left behind
    (DuckDB's COPY still creates an empty-but-valid Parquet file, which we
    remove to match the "no file for an empty result" convention the rest
    of this project's manifest/cache code expects).
    """
    theme = core.type_theme_map[overture_type]
    minx, miny, maxx, maxy = bbox
    source = f"s3://overturemaps-us-west-2/release/{release}/theme={theme}/type={overture_type}/*"

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")
    # Otherwise a large fetch (millions of rows, many minutes) looks
    # identical to a hang -- see the CLI's earlier "make it more verbose" ask.
    con.execute("SET enable_progress_bar=true;")

    # "sources" carries per-record provenance (dataset, license, confidence,
    # timestamps...) not needed for geometry work, and is one of the
    # heaviest fields per row -- dropped via SQL EXCLUDE, not a Python-side
    # column list, to keep this a single plain query.
    result = con.execute(f"""
        COPY (
            SELECT * EXCLUDE (sources) FROM read_parquet('{source}', hive_partitioning=1)
            WHERE bbox.xmin < {maxx} AND bbox.xmax > {minx}
              AND bbox.ymin < {maxy} AND bbox.ymax > {miny}
        ) TO '{out_path}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {ROW_GROUP_SIZE})
    """)
    count = result.fetchone()[0]
    if count == 0:
        out_path.unlink(missing_ok=True)
    return count
