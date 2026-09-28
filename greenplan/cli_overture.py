"""`greenplan overture ...` -- populate and inspect the shared local Overture
cache (see CLAUDE.md's "Overture cache" design). This is a cache-maintenance
tool, meant to be run manually or on a schedule (weekly/monthly) -- it is
NOT invoked as part of `parse`/`plant`, and per-project use of this cache
(querying it by a project's real post-georeferencing bbox) is separate,
not-yet-implemented work.
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Optional

import typer
from overturemaps import core as overture_core

from greenplan.overture.cache import is_fresh, latest_fetch_per_type, manifest_path
from greenplan.overture.client import (
    DEFAULT_CACHE_TYPES,
    DEFAULT_MOSCOW_BBOX,
    fetch_type_to_parquet,
    log,
)

overture_app = typer.Typer(help="Populate/inspect the shared local Overture Maps cache.")

_DEFAULT_BBOX_STR = ",".join(str(c) for c in DEFAULT_MOSCOW_BBOX)
_DEFAULT_TYPES_STR = ",".join(DEFAULT_CACHE_TYPES)
_ALL_TYPES = overture_core.get_all_overture_types()


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 4:
        raise typer.BadParameter("bbox must be 'minx,miny,maxx,maxy' (WGS84 lon/lat)")
    try:
        minx, miny, maxx, maxy = (float(p) for p in parts)
    except ValueError as ex:
        raise typer.BadParameter(f"bbox values must be numeric: {ex}")
    return (minx, miny, maxx, maxy)


def _parse_types(value: str) -> list[str]:
    types = [t.strip() for t in value.split(",") if t.strip()]
    unknown = sorted(set(types) - set(_ALL_TYPES))
    if unknown:
        raise typer.BadParameter(f"Unknown type(s): {', '.join(unknown)}. Known: {', '.join(_ALL_TYPES)}")
    return types


@overture_app.command()
def fetch(
    bbox: str = typer.Option(
        _DEFAULT_BBOX_STR,
        help="minx,miny,maxx,maxy (WGS84 lon/lat). Default: Moscow within MKAD + margin "
        "(does not cover the New Moscow/TiNAO extension).",
    ),
    types: str = typer.Option(
        _DEFAULT_TYPES_STR,
        help=f"Comma-separated Overture types to fetch. Known: {', '.join(_ALL_TYPES)} "
        "('land_cover' is a known type but excluded from the default set -- see client.py).",
    ),
    release: Optional[str] = typer.Option(None, help="Overture release (default: latest)"),
    out_dir: Path = typer.Option(Path("data/overture_cache"), help="Cache directory"),
    name: str = typer.Option("moscow", help="Label used in cache filenames"),
    max_age_days: float = typer.Option(
        30.0,
        help="Skip a type if it was already fetched more recently than this, at any release "
        "(see overture status). Ignored with --force.",
    ),
    force: bool = typer.Option(False, help="Re-fetch every requested type regardless of freshness"),
) -> None:
    """Fetch (or refresh) the shared Overture cache for a bbox.

    Meant to be run manually or on a schedule -- e.g.
    `greenplan overture fetch` with all defaults refreshes the standard
    Moscow-wide cache. NOT part of per-project processing.
    """
    parsed_bbox = _parse_bbox(bbox)
    parsed_types = _parse_types(types)
    resolved_release = release or overture_core.get_latest_release()

    out_dir.mkdir(parents=True, exist_ok=True)
    already_fresh = {} if force else latest_fetch_per_type(out_dir, name)

    log(f"Release: {resolved_release}")
    log(f"Bbox: {parsed_bbox}")
    log(f"Types ({len(parsed_types)}): {', '.join(parsed_types)}")
    log(f"Output dir: {out_dir.resolve()}\n")

    manifest = {
        "release": resolved_release,
        "bbox": list(parsed_bbox),
        "downloaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "types": {},
    }

    run_start = time.monotonic()
    for i, overture_type in enumerate(parsed_types, start=1):
        fresh_entry = already_fresh.get(overture_type)
        if is_fresh(fresh_entry, max_age_days):
            log(
                f"[{i}/{len(parsed_types)}] '{overture_type}': fresh as of "
                f"{fresh_entry['downloaded_at']} (release {fresh_entry.get('release')}), "
                f"skipping -- pass --force to refetch anyway"
            )
            manifest["types"][overture_type] = {"status": "skipped_fresh", **fresh_entry}
            continue

        log(f"[{i}/{len(parsed_types)}] Fetching '{overture_type}'...")
        type_start = time.monotonic()
        out_path = out_dir / f"{name}-{overture_type}-{resolved_release}.parquet"
        try:
            count = fetch_type_to_parquet(overture_type, parsed_bbox, resolved_release, out_path)
            elapsed = time.monotonic() - type_start
            if count == 0:
                log(f"  0 feature(s) in {elapsed:.1f}s, skipping file write")
                manifest["types"][overture_type] = {
                    "status": "ok", "count": 0, "file": None, "elapsed_seconds": round(elapsed, 1),
                }
                continue
            log(f"  {count} feature(s) in {elapsed:.1f}s -> {out_path}")
            manifest["types"][overture_type] = {
                "status": "ok", "count": count, "file": str(out_path), "elapsed_seconds": round(elapsed, 1),
            }
        except Exception as ex:
            elapsed = time.monotonic() - type_start
            log(f"  FAILED after {elapsed:.1f}s: {ex}")
            manifest["types"][overture_type] = {
                "status": "error", "error": str(ex), "elapsed_seconds": round(elapsed, 1),
            }
    total_elapsed = time.monotonic() - run_start

    manifest_file = manifest_path(out_dir, name, resolved_release)
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

    log(f"\nSummary (total {total_elapsed:.1f}s):")
    for overture_type, info in manifest["types"].items():
        status = info["status"]
        if status == "ok":
            log(f"  [ok]      {overture_type}: {info['count']} feature(s) ({info['elapsed_seconds']}s)")
        elif status == "skipped_fresh":
            log(f"  [fresh]   {overture_type}: skipped (fetched {info['downloaded_at']})")
        else:
            log(f"  [ERROR]   {overture_type}: {info.get('error')}")
    log(f"\nWrote manifest to {manifest_file}")


@overture_app.command()
def status(
    out_dir: Path = typer.Option(Path("data/overture_cache"), help="Cache directory"),
    name: str = typer.Option("moscow", help="Label used in cache filenames"),
) -> None:
    """Report what's in the shared Overture cache and how stale each type is."""
    latest = latest_fetch_per_type(out_dir, name)
    if not latest:
        typer.echo(f"No cache entries found under {out_dir.resolve()} (name={name!r}).")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    for overture_type in sorted(latest):
        entry = latest[overture_type]
        fetched = datetime.datetime.fromisoformat(entry["downloaded_at"])
        age_days = (now - fetched).total_seconds() / 86400
        typer.echo(
            f"{overture_type}: {entry.get('count', '?')} feature(s), release {entry.get('release')}, "
            f"fetched {age_days:.1f}d ago ({entry['downloaded_at']})"
        )

    missing = sorted(set(DEFAULT_CACHE_TYPES) - set(latest))
    if missing:
        typer.echo(f"\nNot yet fetched (in the default set): {', '.join(missing)}")
