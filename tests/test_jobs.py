import os
import time
from dataclasses import replace

import pytest

from fakes import FakeDownloader, ManualExecutor
from youtube_downloader.config import Settings
from youtube_downloader.services.downloader import JobCancelled
from youtube_downloader.services.jobs import JobRegistry, JobState, QueueFull
from youtube_downloader.services.youtube import preset_by_key


@pytest.fixture
def settings(tmp_path):
    return Settings(temp_root=tmp_path / "jobs", max_concurrent_jobs=1, max_queued_jobs=1)


@pytest.fixture
def executor():
    return ManualExecutor()


def make_registry(settings, executor, downloader=None, clock=time.monotonic):
    return JobRegistry(
        downloader or FakeDownloader(), settings, executor=executor, clock=clock
    )


def test_submitted_job_starts_queued(settings, executor):
    registry = make_registry(settings, executor)

    job = registry.submit("dQw4w9WgXcQ", preset_by_key("mp4-720"))

    assert job.state == JobState.QUEUED
    assert registry.get(job.id) is job
    assert len(executor.pending) == 1


def test_job_completes_and_reports_its_file(settings, executor):
    registry = make_registry(settings, executor)
    job = registry.submit("dQw4w9WgXcQ", preset_by_key("mp4-720"))

    executor.run_next()

    assert job.state == JobState.DONE
    assert job.progress == 100.0
    assert job.file_path is not None
    assert job.file_path.exists()
    assert job.file_path.name == "file.mp4"


def test_failure_records_the_error_and_cleans_up(settings, executor):
    from youtube_downloader.services.downloader import DownloadError

    def fail(preset, destination, should_cancel):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "partial.part").write_bytes(b"x")
        raise DownloadError("private", "This video is private.")

    registry = make_registry(settings, executor, FakeDownloader(behaviour=fail))
    job = registry.submit("dQw4w9WgXcQ", preset_by_key("mp4-720"))

    executor.run_next()

    assert job.state == JobState.FAILED
    assert job.error is not None and job.error.code == "private"
    assert not registry.job_dir(job).exists()


def test_cancelling_a_queued_job_never_runs_it(settings, executor):
    downloader = FakeDownloader()
    registry = make_registry(settings, executor, downloader)
    job = registry.submit("dQw4w9WgXcQ", preset_by_key("mp3-192"))

    registry.cancel(job.id)
    executor.run_next()

    assert job.state == JobState.CANCELLED
    assert downloader.calls == []


def test_cancelling_a_running_job_stops_it_on_the_next_hook(settings, executor):
    registry = make_registry(settings, executor)
    holder: dict[str, str] = {}

    def stop_on_second_hook(preset, destination, should_cancel):
        registry.cancel(holder["id"])
        if should_cancel():
            raise JobCancelled()
        return destination / "never.mp4"

    job = registry.submit("dQw4w9WgXcQ", preset_by_key("mp4-720"))
    holder["id"] = job.id
    registry._downloader.behaviour = stop_on_second_hook

    executor.run_next()

    assert job.state == JobState.CANCELLED


def test_queue_full_is_reported(settings, executor):
    registry = make_registry(settings, executor)

    registry.submit("dQw4w9WgXcQ", preset_by_key("mp4-720"))  # queued
    executor.pending = []  # pretend it is now running elsewhere
    registry._running, registry._queued = 1, 1

    with pytest.raises(QueueFull):
        registry.submit("dQw4w9WgXcQ", preset_by_key("mp4-720"))


def test_timeout_fails_the_job_instead_of_hanging(settings, executor):
    ticks = iter([0.0, 500.0, 500.0])
    settings = replace(settings, job_timeout_seconds=1)
    registry = make_registry(
        settings, executor, clock=lambda: next(ticks, 500.0)
    )

    def obey_cancel(preset, destination, should_cancel):
        if should_cancel():
            raise JobCancelled()
        return destination / "never.mp4"

    registry._downloader.behaviour = obey_cancel
    job = registry.submit("dQw4w9WgXcQ", preset_by_key("mp4-720"))

    executor.run_next()

    assert job.state == JobState.FAILED
    assert job.error is not None and job.error.code == "timeout"


def test_sweep_removes_only_stale_directories(settings, executor):
    registry = make_registry(settings, executor)
    settings.temp_root.mkdir(parents=True, exist_ok=True)

    stale = settings.temp_root / "old"
    fresh = settings.temp_root / "new"
    stale.mkdir()
    fresh.mkdir()
    now = time.time()
    os.utime(stale, (now - 3 * 60 * 60, now - 3 * 60 * 60))
    os.utime(fresh, (now, now))

    removed = registry.sweep(older_than_seconds=2 * 60 * 60, now=now)

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()


def test_sweep_is_a_no_op_without_a_temp_root(settings, executor):
    registry = make_registry(settings, executor)

    assert registry.sweep() == 0


def test_discard_files_removes_the_job_directory(settings, executor):
    registry = make_registry(settings, executor)
    job = registry.submit("dQw4w9WgXcQ", preset_by_key("mp4-360"))
    job_dir = registry.job_dir(job)
    job_dir.mkdir(parents=True)
    (job_dir / "leftover.mp4").write_bytes(b"x")

    registry.discard_files(job)

    assert not job_dir.exists()
