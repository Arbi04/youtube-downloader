"""In-memory job registry and executor.

See ``docs/adr/0001-in-process-jobs.md``: no Redis, no Celery, no database.
State is lost on restart, which is acceptable while this app is single-user and
localhost-only.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from youtube_downloader.config import Settings
from youtube_downloader.services.downloader import (
    DownloadError,
    Downloader,
    JobCancelled,
    Progress,
)
from youtube_downloader.services.youtube import Preset

SWEEP_AFTER_SECONDS = 2 * 60 * 60


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = frozenset({JobState.DONE, JobState.FAILED, JobState.CANCELLED})


class QueueFull(Exception):
    """Too many jobs in flight; the caller should back off."""


@dataclass
class Job:
    id: str
    video_id: str
    preset: Preset
    state: JobState = JobState.QUEUED
    progress: float = 0.0
    speed: float | None = None
    eta: int | None = None
    error: DownloadError | None = None
    file_path: Path | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    cancel_requested: bool = False
    timed_out: bool = False

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL_STATES


class JobRegistry:
    def __init__(
        self,
        downloader: Downloader,
        settings: Settings,
        executor: Executor | None = None,
        clock=time.monotonic,
    ) -> None:
        self._downloader = downloader
        self._settings = settings
        self._executor = executor or ThreadPoolExecutor(
            max_workers=settings.max_concurrent_jobs
        )
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._running = 0
        self._queued = 0

    @property
    def capacity(self) -> int:
        """Concurrent slots plus waiting slots."""
        return self._settings.max_concurrent_jobs + self._settings.max_queued_jobs

    def submit(self, video_id: str, preset: Preset) -> Job:
        with self._lock:
            if self._running + self._queued >= self.capacity:
                raise QueueFull("Too many downloads in flight. Try again in a moment.")
            job = Job(id=uuid.uuid4().hex[:12], video_id=video_id, preset=preset)
            self._jobs[job.id] = job
            self._queued += 1

        self._executor.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.finished:
                return job

            job.cancel_requested = True
            if job.state == JobState.QUEUED:
                job.state = JobState.CANCELLED
                self._queued = max(0, self._queued - 1)
            return job

    def job_dir(self, job: Job) -> Path:
        return self._settings.temp_root / job.id

    def discard_files(self, job: Job) -> None:
        shutil.rmtree(self.job_dir(job), ignore_errors=True)

    def sweep(
        self, older_than_seconds: int = SWEEP_AFTER_SECONDS, now: float | None = None
    ) -> int:
        """Remove temp directories left behind by a previous run."""
        current = time.time() if now is None else now
        cutoff = current - older_than_seconds
        root = self._settings.temp_root
        if not root.exists():
            return 0

        removed = 0
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            try:
                modified = entry.stat().st_mtime
            except OSError:
                continue
            if modified < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed

    def _run(self, job: Job) -> None:
        with self._lock:
            self._queued = max(0, self._queued - 1)
            if job.cancel_requested or job.state == JobState.CANCELLED:
                job.state = JobState.CANCELLED
                return
            job.state = JobState.RUNNING
            job.started_at = self._clock()
            self._running += 1

        destination = self.job_dir(job)

        def on_progress(progress: Progress) -> None:
            with self._lock:
                job.progress = progress.percent
                job.speed = progress.speed
                job.eta = progress.eta

        def should_cancel() -> bool:
            with self._lock:
                if job.cancel_requested:
                    return True
                started = (
                    job.started_at if job.started_at is not None else self._clock()
                )
                if self._clock() - started > self._settings.job_timeout_seconds:
                    job.timed_out = True
                    return True
            return False

        try:
            path = self._downloader.download(
                job.video_id, job.preset, destination, on_progress, should_cancel
            )
        except JobCancelled:
            with self._lock:
                if job.timed_out:
                    job.state = JobState.FAILED
                    job.error = DownloadError(
                        "timeout", "The download took too long and was stopped."
                    )
                else:
                    job.state = JobState.CANCELLED
            self.discard_files(job)
        except DownloadError as exc:
            with self._lock:
                job.state = JobState.FAILED
                job.error = exc
            self.discard_files(job)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as text
            with self._lock:
                job.state = JobState.FAILED
                job.error = DownloadError("unknown", f"Download failed: {exc}")
            self.discard_files(job)
        else:
            with self._lock:
                job.state = JobState.DONE
                job.progress = 100.0
                job.speed = None
                job.eta = 0
                job.file_path = path
        finally:
            with self._lock:
                self._running = max(0, self._running - 1)
