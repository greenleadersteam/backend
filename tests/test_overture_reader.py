import json

import duckdb
import pytest

from greenplan.overture.reader import read_bbox, resolve_cache_file


@pytest.fixture
def cache_dir(tmp_path):
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial; LOAD spatial;")
    except duckdb.Error as ex:
        pytest.skip(f"DuckDB spatial extension unavailable: {ex}")
    out = tmp_path / "moscow-building-r1.parquet"
    con.execute(f"""
        COPY (
            SELECT * FROM (VALUES
                ('near', 'school', ST_GeomFromText('POLYGON((37.60 55.70, 37.61 55.70, 37.61 55.71, 37.60 55.71, 37.60 55.70))')),
                ('far', NULL, ST_GeomFromText('POLYGON((38.00 56.00, 38.01 56.00, 38.01 56.01, 38.00 56.01, 38.00 56.00))'))
            ) t(id, class, geometry)
        ) TO '{out}' (FORMAT PARQUET)
    """)
    con.execute(f"""
        COPY (SELECT *, {{'xmin': ST_XMin(geometry), 'xmax': ST_XMax(geometry),
                         'ymin': ST_YMin(geometry), 'ymax': ST_YMax(geometry)}} AS bbox
              FROM '{out}') TO '{out}.tmp' (FORMAT PARQUET)
    """)
    (tmp_path / "moscow-building-r1.parquet.tmp").rename(out)
    manifest = {
        "release": "r1",
        "downloaded_at": "2026-09-28T00:00:00+00:00",
        # the manifest's path is relative to wherever `overture fetch` ran
        "types": {"building": {"status": "ok", "count": 2, "file": "some/other/dir/moscow-building-r1.parquet"}},
    }
    (tmp_path / "moscow-manifest-r1.json").write_text(json.dumps(manifest))
    return tmp_path


def test_resolve_cache_file_reanchors_to_cache_dir(cache_dir):
    path, release = resolve_cache_file(cache_dir, "moscow", "building")
    assert path == cache_dir / "moscow-building-r1.parquet"
    assert release == "r1"
    assert resolve_cache_file(cache_dir, "moscow", "land_use") is None


def test_read_bbox_filters_by_bbox(cache_dir):
    path, _ = resolve_cache_file(cache_dir, "moscow", "building")
    rows = read_bbox(path, (37.5, 55.6, 37.7, 55.8), ["id", "class AS cls"])
    assert [r["id"] for r, _ in rows] == ["near"]
    record, geom = rows[0]
    assert record["cls"] == "school"
    assert geom.bounds == pytest.approx((37.60, 55.70, 37.61, 55.71))
