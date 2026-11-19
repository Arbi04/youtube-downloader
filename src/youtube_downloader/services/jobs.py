"""In-memory job registry, batch runner, and archive builder.

See ``docs/adr/0001-in-process-jobs.md`` for the single-job model and
``docs/adr/0002-batches.md`` for why a Batch takes one worker slot and runs its
items one after another. State is lost on restart, which is acceptable while
this app is single-user and localhost-only.
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
import zipfile
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

from youtube_downloader.config import Settings
from youtube_downloader.services.downloader import (
    DownloadError,
    Downloader,
    JobCancelled,
    Progress,
)
from youtube_downloader.services.youtube import Preset

SWEEP_AFTER_SECONDS = 2 * 60 * 60

# Characters Windows refuses in a filename, plus control characters.
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


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
    title: str | None = None
    batch_id: str | None = None
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


@dataclass
class Batch:
    """A playlist download: Jobs that share one slot and run in order."""

    id: str
    playlist_title: str
    preset: Preset
    items: list[Job]
    state: JobState = JobState.QUEUED
    zip_path: Path | None = None
    created_at: float = field(default_factory=time.time)
    cancel_requested: bool = False

    @property
    def finished(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def saved(self) -> list[Job]:
        return [job for job in self.items if job.state == JobState.DONE]

    @property
    def failed(self) -> list[Job]:
        return [job for job in self.items if job.state == JobState.FAILED]

    @property
    def progress(self) -> float:
        if not self.items:
            return 0.0
        if self.finished:
            return 100.0
        settled = sum(1 for job in self.items if job.finished)
        running = next(
            (job.progress for job in self.items if job.state == JobState.RUNNING), 0.0
        )
        return round((settled + running / 100) / len(self.items) * 100, 1)


def safe_name(value: str, fallback: str) -> str:
    """A single path component that every OS accepts, capped for sanity."""
    cleaned = _UNSAFE_NAME.sub("-", value or "").strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80].strip(" .") or fallback


def build_archive(batch: Batch, directory: Path) -> Path | None:
    """Pack the saved items into one archive, preserving playlist order."""
    saved = batch.saved
    if not saved:
        return None

    folder = safe_name(batch.playlist_title, "Playlist")
    archive = directory / f"{folder}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for index, job in enumerate(batch.items, start=1):
            if job.state != JobState.DONE or job.file_path is None:
                continue
            name = job.file_path.name.replace(f" {job.preset.key}", "")
            bundle.write(job.file_path, arcname=f"{folder}/{index:02d} - {name}")
    return archive


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
        self._batches: dict[str, Batch] = {}
        self._lock = threading.RLock()
        self._running = 0
        self._queued = 0

    @property
    def capacity(self) -> int:
        """Concurrent slots plus waiting slots."""
        return self._settings.max_concurrent_jobs + self._settings.max_queued_jobs

    def submit(self, video_id: str, preset: Preset) -> Job:
        with self._lock:
            self._enforce_capacity()
            job = Job(id=uuid.uuid4().hex[:12], video_id=video_id, preset=preset)
            self._jobs[job.id] = job
            self._queued += 1

        self._executor.submit(self._run, job)
        return job

    def submit_batch(
        self,
        playlist_title: str,
        items: Sequence[tuple[str, str]],
        preset: Preset,
    ) -> Batch:
        """Queue a playlist. One Batch costs one slot, not one per item."""
        with self._lock:
            self._enforce_capacity()
            batch = Batch(
                id=uuid.uuid4().hex[:12],
                playlist_title=playlist_title,
                preset=preset,
                items=[
                    Job(
                        id=uuid.uuid4().hex[:12],
                        video_id=video_id,
                        preset=preset,
                        title=title,
                        batch_id="",
                    )
                    for video_id, title in items
                ],
            )
            for job in batch.items:
                job.batch_id = batch.id
            self._batches[batch.id] = batch
            self._queued += 1

        self._executor.submit(self._run_batch, batch)
        return batch

    def _enforce_capacity(self) -> None:
        if self._running + self._queued >= self.capacity:
            raise QueueFull("Too many downloads in flight. Try again in a moment.")

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._lock:
            return self._batches.get(batch_id)

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

    def cancel_batch(self, batch_id: str) -> Batch | None:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None or batch.finished:
                return batch

            batch.cancel_requested = True
            if batch.state == JobState.QUEUED:
                batch.state = JobState.CANCELLED
                for job in batch.items:
                    job.state = JobState.CANCELLED
                self._queued = max(0, self._queued - 1)
            return batch

    def job_dir(self, job: Job) -> Path:
        return self._settings.temp_root / job.id

    def batch_dir(self, batch: Batch) -> Path:
        return self._settings.temp_root / batch.id

    def discard_files(self, job: Job) -> None:
        shutil.rmtree(self.job_dir(job), ignore_errors=True)

    def discard_batch(self, batch: Batch) -> None:
        shutil.rmtree(self.batch_dir(batch), ignore_errors=True)

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
            self._running += 1

        try:
            self._perform(job, self.job_dir(job), is_cancelled=lambda: False)
        finally:
            with self._lock:
                self._running = max(0, self._running - 1)

    def _run_batch(self, batch: Batch) -> None:
        with self._lock:
            self._queued = max(0, self._queued - 1)
            if batch.cancel_requested:
                batch.state = JobState.CANCELLED
                for job in batch.items:
                    job.state = JobState.CANCELLED
                return
            batch.state = JobState.RUNNING
            self._running += 1

        directory = self.batch_dir(batch)

        def cancelled() -> bool:
            with self._lock:
                return batch.cancel_requested

        try:
            for index, job in enumerate(batch.items, start=1):
                if cancelled():
                    with self._lock:
                        if not job.finished:
                            job.state = JobState.CANCELLED
                    continue
                self._perform(job, directory / f"{index:02d}", cancelled)
        finally:
            with self._lock:
                self._running = max(0, self._running - 1)

        with self._lock:
            if batch.cancel_requested:
                batch.state = JobState.CANCELLED
                self.discard_batch(batch)
                return

        if batch.saved:
            batch.zip_path = build_archive(batch, directory)
            batch.state = JobState.DONE
        else:
            batch.state = JobState.FAILED
            self.discard_batch(batch)

    def _perform(
        self,
        job: Job,
        destination: Path,
        is_cancelled: Callable[[], bool],
    ) -> None:
        """Download one item and record its outcome.

        Single Jobs and Batch items share this path on purpose: their progress,
        cancellation, and error reporting must not drift apart.
        """
        with self._lock:
            job.state = JobState.RUNNING
            job.started_at = self._clock()

        def on_progress(progress: Progress) -> None:
            with self._lock:
                job.progress = progress.percent
                job.speed = progress.speed
                job.eta = progress.eta

        def should_cancel() -> bool:
            if is_cancelled():
                return True
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
            shutil.rmtree(destination, ignore_errors=True)
        except DownloadError as exc:
            with self._lock:
                job.state = JobState.FAILED
                job.error = exc
            shutil.rmtree(destination, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as text
            with self._lock:
                job.state = JobState.FAILED
                job.error = DownloadError("unknown", f"Download failed: {exc}")
            shutil.rmtree(destination, ignore_errors=True)
        else:
            with self._lock:
                job.state = JobState.DONE
                job.progress = 100.0
                job.speed = None
                job.eta = 0
                job.file_path = path
