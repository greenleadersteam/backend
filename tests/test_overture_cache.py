import datetime
import json

from greenplan.overture.cache import is_fresh, latest_fetch_per_type, load_all_manifests, manifest_path


def _write_manifest(tmp_path, name, release, downloaded_at, types):
    path = manifest_path(tmp_path, name, release)
    path.write_text(json.dumps({"release": release, "downloaded_at": downloaded_at, "types": types}))
    return path


def test_load_all_manifests_ignores_unrelated_and_corrupt_files(tmp_path):
    _write_manifest(tmp_path, "moscow", "2026-08-19.0", "2026-08-20T00:00:00+00:00", {})
    (tmp_path / "moscow-manifest-corrupt.json").write_text("{not json")
    (tmp_path / "unrelated.json").write_text(json.dumps({"foo": "bar"}))

    manifests = load_all_manifests(tmp_path, "moscow")

    assert len(manifests) == 1
    assert manifests[0]["release"] == "2026-08-19.0"


def test_latest_fetch_per_type_picks_most_recent_across_manifests(tmp_path):
    _write_manifest(
        tmp_path, "moscow", "2026-08-19.0", "2026-08-20T00:00:00+00:00",
        {"building": {"status": "ok", "count": 100}},
    )
    _write_manifest(
        tmp_path, "moscow", "2026-09-23.1", "2026-09-27T00:00:00+00:00",
        {"building": {"status": "ok", "count": 150}, "land_use": {"status": "ok", "count": 5}},
    )

    latest = latest_fetch_per_type(tmp_path, "moscow")

    assert latest["building"]["count"] == 150
    assert latest["building"]["release"] == "2026-09-23.1"
    assert latest["land_use"]["count"] == 5


def test_latest_fetch_per_type_ignores_failed_and_empty_entries(tmp_path):
    _write_manifest(
        tmp_path, "moscow", "2026-08-19.0", "2026-08-20T00:00:00+00:00",
        {
            "segment": {"status": "error", "error": "timed out"},
            "water": {"status": "ok", "count": 0},
        },
    )

    latest = latest_fetch_per_type(tmp_path, "moscow")

    assert latest == {}


def test_is_fresh_within_and_beyond_max_age():
    now = datetime.datetime.now(datetime.timezone.utc)
    recent = {"downloaded_at": (now - datetime.timedelta(days=5)).isoformat()}
    stale = {"downloaded_at": (now - datetime.timedelta(days=45)).isoformat()}

    assert is_fresh(recent, max_age_days=30)
    assert not is_fresh(stale, max_age_days=30)
    assert not is_fresh(None, max_age_days=30)
