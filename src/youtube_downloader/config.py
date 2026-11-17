"""Runtime settings, read from the environment once at startup."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TEMP_DIR = Path(tempfile.gettempdir()) / "youtube-downloader"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


@dataclass(frozen=True)
class Settings:
    """Everything the app needs to know before it starts."""

    port: int = 8000
    temp_root: Path = field(default_factory=lambda: DEFAULT_TEMP_DIR)
    cookies_file: Path | None = None
    max_concurrent_jobs: int = 2
    max_queued_jobs: int = 5
    job_timeout_seconds: int = 900

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError(f"port must be between 1 and 65535, got {self.port}")
        if self.max_concurrent_jobs < 1:
            raise ValueError(
                f"max_concurrent_jobs must be at least 1, got {self.max_concurrent_jobs}"
            )
        if self.max_queued_jobs < 0:
            raise ValueError(
                f"max_queued_jobs cannot be negative, got {self.max_queued_jobs}"
            )
        if self.job_timeout_seconds < 1:
            raise ValueError(
                f"job_timeout_seconds must be at least 1, got {self.job_timeout_seconds}"
            )

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the process environment.

        Missing variables fall back to the defaults above; an empty variable is
        treated as missing, so ``YTDLP_COOKIES_FILE=`` means "no cookies file".
        """
        return cls(
            port=_env_int("PORT", 8000),
            temp_root=_env_path("TEMP_ROOT") or DEFAULT_TEMP_DIR,
            cookies_file=_env_path("YTDLP_COOKIES_FILE"),
            max_concurrent_jobs=_env_int("MAX_CONCURRENT_JOBS", 2),
            max_queued_jobs=_env_int("MAX_QUEUED_JOBS", 5),
            job_timeout_seconds=_env_int("JOB_TIMEOUT_SECONDS", 900),
        )
