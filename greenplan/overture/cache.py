"""Manifest bookkeeping for the shared local Overture cache: what's been
fetched, when, and at what release -- so `greenplan overture status` can
report staleness without re-reading data files, and `greenplan overture
fetch` can skip a type that's still fresh enough (see cli_overture.py).

One manifest file per (name, release) pair, same shape the prototype
download_overture.py already used (release/bbox/downloaded_at/types), so a
cache directory can accumulate manifests across releases over time as it's
refreshed -- `latest_fetch_per_type` reads all of them and picks each
type's most recent successful fetch regardless of which release it came
from, since an operator may reasonably refresh different types at
different times.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path


def manifest_path(out_dir: Path, name: str, release: str) -> Path:
    return out_dir / f"{name}-manifest-{release}.json"


def load_all_manifests(out_dir: Path, name: str) -> list[dict]:
    manifests = []
    if not out_dir.exists():
        return manifests
    for path in sorted(out_dir.glob(f"{name}-manifest-*.json")):
        try:
            manifests.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return manifests


def latest_fetch_per_type(out_dir: Path, name: str) -> dict[str, dict]:
    """{overture_type: {status, count, file, release, downloaded_at}} for
    each type's most recent *successful* (count > 0) fetch across every
    manifest found, or {} if none exist yet.
    """
    latest: dict[str, dict] = {}
    for manifest in load_all_manifests(out_dir, name):
        downloaded_at = manifest.get("downloaded_at")
        release = manifest.get("release")
        if not downloaded_at:
            continue
        for overture_type, info in manifest.get("types", {}).items():
            if info.get("status") != "ok" or not info.get("count"):
                continue
            existing = latest.get(overture_type)
            if existing is None or downloaded_at > existing["downloaded_at"]:
                latest[overture_type] = {**info, "release": release, "downloaded_at": downloaded_at}
    return latest


def is_fresh(entry: dict | None, max_age_days: float) -> bool:
    if entry is None:
        return False
    downloaded_at = entry.get("downloaded_at")
    if not downloaded_at:
        return False
    fetched = datetime.datetime.fromisoformat(downloaded_at)
    age = datetime.datetime.now(datetime.timezone.utc) - fetched
    return age <= datetime.timedelta(days=max_age_days)
