"""yt-dlp wrapper.

This is the **only** module allowed to import ``yt_dlp``. Everything else talks
to the :class:`Downloader` protocol, which is what keeps the test suite offline
and what would make swapping engines a local change.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import yt_dlp

from youtube_downloader.config import Settings
from youtube_downloader.services.youtube import (
    Preset,
    PresetKind,
    RawPlaylistItem,
    RawPlaylistPreview,
    RawPreview,
    format_selector,
    playlist_url,
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

# Codes that usually mean "try again in a moment"; the Batch runner retries them.
TRANSIENT_CODES = frozenset({"forbidden", "network", "rate_limited"})


# yt-dlp colourises its messages; users should never see the escape codes.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

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
        "forbidden",
        ("http error 403", "403: forbidden"),
        "YouTube refused to send this video's data (HTTP 403). This is usually a "
        "temporary block or an expired sign-in, not a broken link — try this video "
        "again. If it keeps failing, export cookies.txt from a signed-in browser.",
    ),
    (
        "rate_limited",
        (
            "http error 429",
            "429: too many requests",
            "too many requests",
            "rate limit",
            "ratelimit",
        ),
        "YouTube is rate-limiting this machine (HTTP 429). Wait a minute before "
        "trying again; a cookies.txt from a signed-in browser also helps.",
    ),
    (
        "network",
        (
            "http error 500",
            "http error 502",
            "http error 503",
            "http error 504",
            "connection reset",
            "connection aborted",
            "connection refused",
            "connection error",
            "remote end closed",
            "incompleteread",
            "timed out",
            "timeout",
            "temporary failure",
            "name or service not known",
            "getaddrinfo failed",
            "network is unreachable",
            "unable to download webpage",
        ),
        "The connection to YouTube failed. Check your internet connection and "
        "try again — this is usually temporary.",
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
        "format_unavailable",
        ("requested format is not available",),
        "No downloadable format matched this video. If YTDLP_COOKIES_FILE is set, "
        "install Node.js — YouTube's newer formats need a JavaScript runtime to unlock.",
    ),
    (
        "unavailable",
        (
            "video unavailable",
            "video is unavailable",
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

    text = _ANSI_ESCAPE.sub("", str(exc)).lower()
    for code, patterns, message in _ERROR_RULES:
        if any(pattern in text for pattern in patterns):
            return DownloadError(code, message)

    detail = _ANSI_ESCAPE.sub("", str(exc)).strip() or type(exc).__name__
    detail = re.sub(r"^ERROR:\s*", "", detail, flags=re.IGNORECASE)
    return DownloadError("unknown", f"Download failed: {detail}")


class Downloader(Protocol):
    def preview(self, video_id: str) -> RawPreview: ...

    def playlist_preview(self, playlist_id: str) -> RawPlaylistPreview: ...

    def download(
        self,
        video_id: str,
        preset: Preset,
        destination: Path,
        on_progress: ProgressCallback,
        should_cancel: CancelCheck,
    ) -> Path: ...


JS_RUNTIME_CANDIDATES = ("deno", "node", "bun", "quickjs")

# A merged download writes one temporary file per stream, named "... f<id>.<ext>".
_MERGED_PART = re.compile(r"\.f[\w-]+\.[A-Za-z0-9]+$")

# Nothing reads as complete until the job itself is done.
_CAP_BEFORE_DONE = 99.0


def _is_merged_part(filename: str | None) -> bool:
    """True when yt-dlp is writing one stream of a two-stream download."""
    return bool(filename and _MERGED_PART.search(Path(filename).name))



def detect_js_runtimes() -> dict[str, dict]:
    """Pick the first JavaScript runtime on PATH.

    yt-dlp probes only Deno by default, but most machines ship Node. A runtime
    is not optional once cookies are configured: YouTube then serves formats
    behind the n-sig challenge, and without a solver the video has no streams.
    """
    for name in JS_RUNTIME_CANDIDATES:
        if shutil.which(name):
            return {name: {}}
    return {}


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

        runtimes = detect_js_runtimes()
        if runtimes:
            options["js_runtimes"] = runtimes

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

    def playlist_preview(self, playlist_id: str) -> RawPlaylistPreview:
        """List a playlist's items without touching each video's metadata.

        Flat extraction returns ids, titles, and durations in one request. Asking
        for full metadata per entry would make a 25-item Preview slower than the
        download, and only availability would improve — see ADR-0002.
        """
        options = {
            **self._base_options(),
            "skip_download": True,
            "extract_flat": "in_playlist",
            "noplaylist": False,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(playlist_url(playlist_id), download=False)
        except Exception as exc:  # noqa: BLE001 - translated below
            raise map_error(exc) from exc

        items = tuple(
            RawPlaylistItem(
                video_id=entry["id"],
                title=entry.get("title") or entry["id"],
                duration_seconds=entry.get("duration"),
            )
            for entry in (info.get("entries") or [])
            if entry and entry.get("id")
        )
        return RawPlaylistPreview(
            playlist_id=playlist_id,
            title=info.get("title") or "Playlist",
            items=items,
        )

    def output_template(self, preset: Preset) -> str:
        """Filename template: readable title, capped, with the preset baked in."""
        return f"%(title).120B [%(id)s] {preset.key}.%(ext)s"

    def make_progress_hook(
        self,
        on_progress: ProgressCallback,
        should_cancel: CancelCheck,
        streams: int = 1,
    ) -> Callable[[dict], None]:
        """Progress for one job, as a single number that never goes backwards.

        yt-dlp reports per stream: a 720p download reports the video file
        reaching 100%, then starts the audio file again at 0% — and after every
        stream it asks for a "park at 99 while ffmpeg works" update. Reported
        raw, that fills the bar, drops it, and refills it. So each stream gets
        its own slice of the bar, and the total is only shown as complete when
        the job itself is.
        """
        count = max(1, streams)
        index = 0
        shown = 0.0

        def report(percent: float, speed: float | None, eta: int | None) -> None:
            nonlocal shown
            shown = max(shown, min(percent, _CAP_BEFORE_DONE))
            on_progress(Progress(percent=round(shown, 1), speed=speed, eta=eta))

        def hook(status: dict) -> None:
            nonlocal count, index
            if should_cancel():
                raise JobCancelled()

            state = status.get("status")
            if state == "downloading":
                if count > 1 and index == 0 and not _is_merged_part(
                    status.get("filename")
                ):
                    # The selector fell back to one progressive stream.
                    count = 1
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                downloaded = status.get("downloaded_bytes") or 0
                within = (downloaded / total) if total else 0.0
                report(
                    (index + within) / count * 100,
                    status.get("speed"),
                    status.get("eta"),
                )
            elif state == "finished":
                index += 1
                report(index / count * 100, None, 0)

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
            "progress_hooks": [
                self.make_progress_hook(
                    on_progress,
                    should_cancel,
                    streams=2 if preset.kind == PresetKind.VIDEO else 1,
                )
            ],
            "overwrites": True,
        }
        if preset.kind == PresetKind.AUDIO:
            options["writethumbnail"] = True
            options["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(preset.bitrate or 192),
                },
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
