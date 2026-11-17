"""yt-dlp wrapper.

This is the **only** module allowed to import ``yt_dlp``. Everything else talks
to the :class:`Downloader` protocol, which is what keeps the test suite offline
and what would make swapping engines a local change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import yt_dlp

from youtube_downloader.config import Settings
from youtube_downloader.services.youtube import (
    Preset,
    PresetKind,
    RawPreview,
    format_selector,
    watch_url,
)


class JobCancelled(Exception):
    """Raised from a progress hook after the user asks to cancel."""


class DownloadError(Exception):
    """A download failure translated into something the user can act on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Progress:
    percent: float
    speed: float | None
    eta: int | None


ProgressCallback = Callable[[Progress], None]
CancelCheck = Callable[[], bool]


# (code, message patterns matched against the lowercased exception text, advice)
_ERROR_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "bot_check",
        (
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "sign in to confirm",
            "cookies for the authentication",
            "login required",
            "use --cookies",
        ),
        "YouTube wants proof this isn't a bot. Set YTDLP_COOKIES_FILE to a "
        "cookies.txt exported from a signed-in browser and try again.",
    ),
    (
        "age_restricted",
        ("age-restricted", "age restricted", "confirm your age", "inappropriate for some users"),
        "This video is age-restricted. A cookies.txt from a signed-in browser is required.",
    ),
    (
        "private",
        ("private video", "this video is private", "members-only"),
        "This video is private, so it cannot be downloaded.",
    ),
    (
        "geo_blocked",
        ("available in your country", "geo restrict", "blocked it in your country"),
        "The uploader or YouTube has blocked this video in your region.",
    ),
    (
        "unavailable",
        (
            "video unavailable",
            "has been removed",
            "does not exist",
            "no longer available",
            "terminated",
            "is not available",
        ),
        "This video is unavailable — it may have been removed or made private.",
    ),
    (
        "ffmpeg_missing",
        ("ffmpeg not found", "ffprobe not found", "ffmpeg is not installed"),
        "ffmpeg is required to merge video and audio. Install it and make sure it is on PATH.",
    ),
)


def map_error(exc: Exception) -> DownloadError:
    """Translate an engine failure into a code and an actionable message."""
    if isinstance(exc, DownloadError):
        return exc

    text = str(exc).lower()
    for code, patterns, message in _ERROR_RULES:
        if any(pattern in text for pattern in patterns):
            return DownloadError(code, message)

    detail = str(exc).strip() or type(exc).__name__
    return DownloadError("unknown", f"Download failed: {detail}")


class Downloader(Protocol):
    def preview(self, video_id: str) -> RawPreview: ...

    def download(
        self,
        video_id: str,
        preset: Preset,
        destination: Path,
        on_progress: ProgressCallback,
        should_cancel: CancelCheck,
    ) -> Path: ...


class YtDlpDownloader:
    """The real engine, configured once from :class:`Settings`."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _base_options(self) -> dict:
        options: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "windowsfilenames": True,
        }
        if self._settings.cookies_file:
            options["cookiefile"] = str(self._settings.cookies_file)
        return options

    def preview(self, video_id: str) -> RawPreview:
        options = {**self._base_options(), "skip_download": True}
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(watch_url(video_id), download=False)
        except Exception as exc:  # noqa: BLE001 - translated below
            raise map_error(exc) from exc

        formats = info.get("formats") or []
        heights = {
            int(fmt["height"])
            for fmt in formats
            if fmt.get("height") and fmt.get("vcodec") not in (None, "none")
        }
        return RawPreview(
            video_id=video_id,
            title=info.get("title") or video_id,
            duration_seconds=info.get("duration"),
            thumbnail_url=info.get("thumbnail"),
            heights=tuple(sorted(heights)),
        )

    def output_template(self, preset: Preset) -> str:
        """Filename template: readable title, capped, with the preset baked in."""
        return f"%(title).120B [%(id)s] {preset.key}.%(ext)s"

    def make_progress_hook(
        self, on_progress: ProgressCallback, should_cancel: CancelCheck
    ) -> Callable[[dict], None]:
        def hook(status: dict) -> None:
            if should_cancel():
                raise JobCancelled()

            state = status.get("status")
            if state == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                downloaded = status.get("downloaded_bytes") or 0
                percent = (downloaded / total * 100) if total else 0.0
                on_progress(
                    Progress(
                        percent=round(percent, 1),
                        speed=status.get("speed"),
                        eta=status.get("eta"),
                    )
                )
            elif state == "finished":
                # Postprocessing (merge/transcode) still runs after this point.
                on_progress(Progress(percent=99.0, speed=None, eta=0))

        return hook

    def download(
        self,
        video_id: str,
        preset: Preset,
        destination: Path,
        on_progress: ProgressCallback,
        should_cancel: CancelCheck,
    ) -> Path:
        destination.mkdir(parents=True, exist_ok=True)

        options: dict = {
            **self._base_options(),
            "format": format_selector(preset),
            "outtmpl": self.output_template(preset),
            "paths": {"home": str(destination)},
            "progress_hooks": [self.make_progress_hook(on_progress, should_cancel)],
            "overwrites": True,
        }
        if preset.kind == PresetKind.AUDIO:
            options["writethumbnail"] = True
            options["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ]
        else:
            options["merge_output_format"] = "mp4"

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(watch_url(video_id), download=True)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - translated below
            raise map_error(exc) from exc

        return self._resolve_output(destination, preset, info)

    @staticmethod
    def _resolve_output(destination: Path, preset: Preset, info: dict | None) -> Path:
        for entry in (info or {}).get("requested_downloads") or []:
            for key in ("filepath", "_filename"):
                value = entry.get(key)
                if value and Path(value).exists():
                    return Path(value)

        # Postprocessors can rename the file, so fall back to the newest match.
        matches = sorted(
            destination.glob(f"*{preset.key}*.{preset.extension}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not matches:
            raise DownloadError(
                "unknown", "The download finished but no file was produced."
            )
        return matches[0]
