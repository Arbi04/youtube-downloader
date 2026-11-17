"""YouTube URL parsing, Preview, and Preset knowledge.

Pure logic: no network, no ffmpeg, no yt-dlp. Every rejectable input here is a
test case, and the host allowlist doubles as the SSRF boundary — nothing outside
it is ever fetched.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, replace
from enum import Enum
from urllib.parse import parse_qs, urlparse

ALLOWED_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)

# Path prefixes that carry the video id in the following segment.
_ID_SEGMENTS = frozenset({"shorts", "embed", "live", "v"})

_VIDEO_ID_LENGTH = 11
_VIDEO_ID_CHARS = frozenset(string.ascii_letters + string.digits + "-_")


class InvalidUrl(ValueError):
    """The input is not a single YouTube video URL."""


class UnknownPreset(ValueError):
    """The preset key does not exist in the ladder."""


def watch_url(video_id: str) -> str:
    """Canonical URL handed to yt-dlp."""
    return f"https://www.youtube.com/watch?v={video_id}"


def _is_video_id(value: str) -> bool:
    return len(value) == _VIDEO_ID_LENGTH and set(value) <= _VIDEO_ID_CHARS


def parse_video_url(raw: str) -> str:
    """Return the 11-character video id, or raise :class:`InvalidUrl`."""
    text = (raw or "").strip()
    if not text:
        raise InvalidUrl("Paste a YouTube link first.")

    if "://" in text:
        candidate = text
    elif ":" in text.split("/")[0]:
        # An opaque scheme such as javascript: or data: — never a real link.
        candidate = text
    else:
        candidate = f"https://{text}"

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise InvalidUrl("Only http and https links are supported.")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise InvalidUrl("That is not a YouTube link.")

    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
    else:
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        if not video_id:
            segments = [segment for segment in parsed.path.split("/") if segment]
            if len(segments) >= 2 and segments[0] in _ID_SEGMENTS:
                video_id = segments[1]

    if not _is_video_id(video_id):
        raise InvalidUrl("That link does not point at a single video.")

    return video_id


class PresetKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"


@dataclass(frozen=True)
class Preset:
    """A user-facing download target, keyed by what the user actually wants."""

    key: str
    label: str
    kind: PresetKind
    height: int | None = None
    available: bool = True

    @property
    def extension(self) -> str:
        return "mp3" if self.kind == PresetKind.AUDIO else "mp4"


PRESETS: tuple[Preset, ...] = (
    Preset("mp4-360", "MP4 360p", PresetKind.VIDEO, height=360),
    Preset("mp4-720", "MP4 720p", PresetKind.VIDEO, height=720),
    Preset("mp4-1080", "MP4 1080p", PresetKind.VIDEO, height=1080),
    Preset("mp3-192", "MP3 192k", PresetKind.AUDIO),
)

PRESETS_BY_KEY = {preset.key: preset for preset in PRESETS}


def preset_by_key(key: str) -> Preset:
    try:
        return PRESETS_BY_KEY[key]
    except KeyError:
        raise UnknownPreset(f"Unknown preset: {key!r}") from None


@dataclass(frozen=True)
class RawPreview:
    """Metadata exactly as the engine reports it, before any product decisions."""

    video_id: str
    title: str
    duration_seconds: int | None
    thumbnail_url: str | None
    heights: tuple[int, ...]


@dataclass(frozen=True)
class Preview:
    """Metadata shaped for the UI."""

    video_id: str
    title: str
    duration_seconds: int | None
    thumbnail_url: str | None
    max_height: int | None


def build_preview(raw: RawPreview) -> Preview:
    heights = [height for height in raw.heights if height]
    return Preview(
        video_id=raw.video_id,
        title=raw.title,
        duration_seconds=raw.duration_seconds,
        thumbnail_url=raw.thumbnail_url,
        max_height=max(heights) if heights else None,
    )


def available_presets(preview: Preview) -> list[Preset]:
    """Every preset, with availability decided by the tallest stream.

    Unknown height means "let yt-dlp fall back", not "nothing is available" —
    hiding every button because metadata was thin would be worse than a preset
    that quietly resolves to a smaller file.
    """

    def is_available(preset: Preset) -> bool:
        if preset.kind == PresetKind.AUDIO:
            return True
        if preview.max_height is None:
            return True
        return preview.max_height >= (preset.height or 0)

    return [replace(preset, available=is_available(preset)) for preset in PRESETS]


def format_selector(preset: Preset) -> str:
    """yt-dlp format string for a preset.

    H.264 + AAC first so the file plays in Windows Media Player, QuickTime,
    phones, and editors; then any codec; then a progressive stream as a last
    resort for videos that only ship one.
    """
    if preset.kind == PresetKind.AUDIO:
        return "bestaudio[ext=m4a]/bestaudio/best"

    height = preset.height
    return (
        f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={height}]+bestaudio"
        f"/best[height<={height}]"
    )
