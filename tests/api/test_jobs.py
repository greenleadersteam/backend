import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from greenplan.api import jobs
from greenplan.api.jobs import JobManager
from greenplan.io.dxf_source import NoDxfFilesError, RootDetectionError


def _poll_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_try_acquire_respects_max_concurrent():
    manager = JobManager(ThreadPoolExecutor(max_workers=2), max_concurrent=2, job_fn=lambda p: None)

    assert manager.try_acquire()
    assert manager.try_acquire()
    assert not manager.try_acquire()

    manager.release()
    assert manager.try_acquire()


def test_submit_runs_job_fn_and_releases_slot_on_completion(tmp_path):
    calls = []

    def fake_job(project_dir):
        calls.append(project_dir)

    manager = JobManager(ThreadPoolExecutor(max_workers=1), max_concurrent=1, job_fn=fake_job)
    assert manager.try_acquire()
    future = manager.submit(tmp_path)
    future.result(timeout=5)

    assert calls == [tmp_path]
    assert _poll_until(lambda: manager.try_acquire()), "slot was not released after job completion"


def test_manager_records_failure_if_job_fn_raises_without_writing_job_yaml(tmp_path):
    """run_processing_job always catches its own errors and writes a failed
    job.yaml itself; this covers the defensive fallback for the case where
    the worker dies before that handler runs at all (e.g. process killed).
    """
    started_at = jobs._now()
    jobs._write_stage(tmp_path, stage=jobs.STAGE_PARSING, started_at=started_at)

    def crashing_job(project_dir):
        raise RuntimeError("boom")

    manager = JobManager(ThreadPoolExecutor(max_workers=1), max_concurrent=1, job_fn=crashing_job)
    manager.try_acquire()
    future = manager.submit(tmp_path)
    with pytest.raises(RuntimeError):
        future.result(timeout=5)

    assert _poll_until(lambda: jobs.read_job_record(tmp_path).stage == jobs.STAGE_FAILED)
    record = jobs.read_job_record(tmp_path)
    assert record.error.code == jobs.UploadErrorCode.OTHER
    assert "boom" in record.error.message


def test_manager_does_not_overwrite_a_terminal_job_yaml_on_crash(tmp_path):
    """If the job function already wrote 'ready'/'failed' itself before an
    unrelated exception surfaces (shouldn't normally happen, but the
    callback must not clobber a real result if it does).
    """
    started_at = jobs._now()

    def job_that_finishes_then_raises(project_dir):
        jobs._write_stage(project_dir, stage=jobs.STAGE_READY, started_at=started_at)
        raise RuntimeError("late, unrelated error")

    manager = JobManager(ThreadPoolExecutor(max_workers=1), max_concurrent=1, job_fn=job_that_finishes_then_raises)
    manager.try_acquire()
    future = manager.submit(tmp_path)
    with pytest.raises(RuntimeError):
        future.result(timeout=5)

    time.sleep(0.1)  # let the done-callback run
    assert jobs.read_job_record(tmp_path).stage == jobs.STAGE_READY


def test_run_processing_job_writes_failed_on_bad_upload(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "processed").mkdir()
    (tmp_path / "upload.zip").write_bytes(b"not a zip file")

    jobs.run_processing_job(tmp_path)

    record = jobs.read_job_record(tmp_path)
    assert record.stage == jobs.STAGE_FAILED
    assert record.error.code == jobs.UploadErrorCode.BAD_ARCHIVE


def test_reconcile_interrupted_jobs_marks_non_terminal_stages_failed(tmp_path):
    projects_root = tmp_path / "projects"
    stuck = projects_root / "stuck"
    stuck.mkdir(parents=True)
    jobs._write_stage(stuck, stage=jobs.STAGE_PARSING, started_at=jobs._now())

    done = projects_root / "done"
    done.mkdir(parents=True)
    jobs._write_stage(done, stage=jobs.STAGE_READY, started_at=jobs._now())

    draft = projects_root / "draft"
    draft.mkdir(parents=True)

    jobs.reconcile_interrupted_jobs(projects_root)

    assert jobs.read_job_record(stuck).stage == jobs.STAGE_FAILED
    assert "restart" in jobs.read_job_record(stuck).error.message.lower()
    assert jobs.read_job_record(done).stage == jobs.STAGE_READY
    assert jobs.read_job_record(draft) is None


def test_current_status_is_draft_when_no_job_yaml(tmp_path):
    assert jobs.current_status(tmp_path) == jobs.DRAFT_STATUS


def test_mark_queued_writes_queued_stage(tmp_path):
    jobs.mark_queued(tmp_path)
    assert jobs.current_status(tmp_path) == jobs.STAGE_QUEUED


def test_classify_error_bad_zip_file(tmp_path):
    garbage = tmp_path / "not-a-zip.zip"
    garbage.write_bytes(b"this is not a zip file")

    try:
        zipfile.ZipFile(garbage)
    except zipfile.BadZipFile as exc:
        error = jobs._classify_error(exc, tmp_path)
    else:
        pytest.fail("expected zipfile.BadZipFile")
    assert error.code == jobs.UploadErrorCode.BAD_ARCHIVE
    assert error.candidates is None


def test_classify_error_unsafe_archive_is_also_bad_archive(tmp_path):
    exc = jobs.UnsafeArchiveError("Unsafe path in archive: '../evil.dxf'")
    error = jobs._classify_error(exc, tmp_path)
    assert error.code == jobs.UploadErrorCode.BAD_ARCHIVE


def test_classify_error_no_dxf_files(tmp_path):
    exc = NoDxfFilesError(f"No .dxf files found under {tmp_path}")
    error = jobs._classify_error(exc, tmp_path)
    assert error.code == jobs.UploadErrorCode.NO_DXF_FOUND
    assert error.candidates is None


def test_classify_error_ambiguous_root_relativizes_candidates_to_raw_dir(tmp_path):
    raw_dir = tmp_path / "raw"
    (raw_dir / "sub").mkdir(parents=True)
    a = raw_dir / "sub" / "genplan.dxf"
    b = raw_dir / "dendroplan.dxf"
    a.touch()
    b.touch()

    exc = RootDetectionError("ambiguous", candidates=[a, b])
    error = jobs._classify_error(exc, raw_dir)

    assert error.code == jobs.UploadErrorCode.AMBIGUOUS_ROOT_DXF
    assert set(error.candidates) == {"sub/genplan.dxf", "dendroplan.dxf"}


def test_classify_error_ambiguous_root_falls_back_to_name_outside_raw_dir(tmp_path):
    """Defensive fallback: a candidate path that (for whatever reason) isn't
    actually under raw_dir should still report *something* usable rather
    than raising -- its bare file name -- instead of crashing the job's own
    error-reporting path.
    """
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    elsewhere = tmp_path / "elsewhere" / "weird.dxf"

    exc = RootDetectionError("ambiguous", candidates=[elsewhere])
    error = jobs._classify_error(exc, raw_dir)

    assert error.candidates == ["weird.dxf"]


def test_read_job_record_upgrades_legacy_plain_string_error(tmp_path):
    """Regression test: job.yaml files written before `error` became a
    structured JobError stored it as a bare string. reconcile_interrupted_jobs
    reads every project's job.yaml at server startup, so a record in this old
    format must not fail validation (that would crash startup for every
    project, not just the one with an old-format record).
    """
    jobs._write_stage(tmp_path, stage=jobs.STAGE_FAILED, started_at=jobs._now())
    from greenplan.api._yamlio import atomic_write_yaml

    raw = jobs._job_path(tmp_path)
    data = jobs.read_yaml(raw)
    data["error"] = "SystemExit: No .dxf files found under /some/raw"
    atomic_write_yaml(raw, data)

    record = jobs.read_job_record(tmp_path)
    assert record.error.code == jobs.UploadErrorCode.OTHER
    assert record.error.message == "SystemExit: No .dxf files found under /some/raw"


def test_classify_error_other_fallback(tmp_path):
    error = jobs._classify_error(RuntimeError("something else entirely"), tmp_path)
    assert error.code == jobs.UploadErrorCode.OTHER
    assert "something else entirely" in error.message
