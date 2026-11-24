"""YouTube URL parsing, Preview, and Preset knowledge.

Pure logic: no network, no ffmpeg, no yt-dlp. Every rejectable input here is a
test case, and the host allowlist doubles as the SSRF boundary — nothing outside
it is ever fetched.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable
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

# Real ids vary: PL…, UU…, RD…, OLAK5uy_…, and short ones like WL.
_PLAYLIST_ID = re.compile(r"^[A-Za-z0-9_-]{2,100}$")

# A Batch is capped by temp disk and the user's patience, not by YouTube.
MAX_BATCH_ITEMS = 25


class InvalidUrl(ValueError):
    """The input is not a YouTube video or playlist URL."""


class UnknownPreset(ValueError):
    """The preset key does not exist in the ladder."""


@dataclass(frozen=True)
class VideoTarget:
    video_id: str


@dataclass(frozen=True)
class PlaylistTarget:
    playlist_id: str


Target = VideoTarget | PlaylistTarget


def watch_url(video_id: str) -> str:
    """Canonical URL handed to yt-dlp."""
    return f"https://www.youtube.com/watch?v={video_id}"


def playlist_url(playlist_id: str) -> str:
    """Canonical playlist URL handed to yt-dlp."""
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _is_video_id(value: str) -> bool:
    return len(value) == _VIDEO_ID_LENGTH and set(value) <= _VIDEO_ID_CHARS


def _as_url(text: str) -> str:
    if "://" in text:
        return text
    if ":" in text.split("/")[0]:
        # An opaque scheme such as javascript: or data: — never a real link.
        return text
    return f"https://{text}"


def _video_id_from(parsed, host: str) -> str:
    if host == "youtu.be":
        return parsed.path.lstrip("/").split("/")[0]

    video_id = parse_qs(parsed.query).get("v", [""])[0]
    if video_id:
        return video_id

    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) >= 2 and segments[0] in _ID_SEGMENTS:
        return segments[1]
    return ""


def parse_target(raw: str) -> Target:
    """Decide what the user pasted: one video, or a playlist.

    ``watch?v=…&list=…`` stays a :class:`VideoTarget` on purpose — a link copied
    from a video page must never silently turn into a 25-item Batch.
    """
    text = (raw or "").strip()
    if not text:
        raise InvalidUrl("Paste a YouTube link first.")

    parsed = urlparse(_as_url(text))
    if parsed.scheme not in {"http", "https"}:
        raise InvalidUrl("Only http and https links are supported.")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise InvalidUrl("That is not a YouTube link.")

    if parsed.path.rstrip("/") == "/playlist":
        playlist_id = parse_qs(parsed.query).get("list", [""])[0]
        if not _PLAYLIST_ID.match(playlist_id):
            raise InvalidUrl("That playlist link is missing a valid list id.")
        return PlaylistTarget(playlist_id)

    video_id = _video_id_from(parsed, host)
    if not _is_video_id(video_id):
        raise InvalidUrl("That link does not point at a single video.")
    return VideoTarget(video_id)


def parse_video_url(raw: str) -> str:
    """Return the 11-character video id, or raise :class:`InvalidUrl`."""
    target = parse_target(raw)
    if isinstance(target, PlaylistTarget):
        raise InvalidUrl("That is a playlist link, not a single video.")
    return target.video_id


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
    bitrate: int | None = None
    available: bool = True

    @property
    def extension(self) -> str:
        return "mp3" if self.kind == PresetKind.AUDIO else "mp4"


PRESETS: tuple[Preset, ...] = (
    Preset("mp4-360", "MP4 360p", PresetKind.VIDEO, height=360),
    Preset("mp4-480", "MP4 480p", PresetKind.VIDEO, height=480),
    Preset("mp4-720", "MP4 720p", PresetKind.VIDEO, height=720),
    Preset("mp4-1080", "MP4 1080p", PresetKind.VIDEO, height=1080),
    Preset("mp4-1440", "MP4 1440p", PresetKind.VIDEO, height=1440),
    Preset("mp3-128", "MP3 128k", PresetKind.AUDIO, bitrate=128),
    Preset("mp3-192", "MP3 192k", PresetKind.AUDIO, bitrate=192),
    Preset("mp3-320", "MP3 320k", PresetKind.AUDIO, bitrate=320),
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


@dataclass(frozen=True)
class RawPlaylistItem:
    """One entry of a playlist, as reported by a flat extraction."""

    video_id: str
    title: str
    duration_seconds: int | None


@dataclass(frozen=True)
class RawPlaylistPreview:
    playlist_id: str
    title: str
    items: tuple[RawPlaylistItem, ...]


@dataclass(frozen=True)
class PlaylistPreview:
    """A playlist shaped for the UI: the first page of items, and how many exist."""

    playlist_id: str
    title: str
    items: tuple[RawPlaylistItem, ...]
    total: int

    @property
    def truncated(self) -> bool:
        return self.total > len(self.items)


def build_playlist_preview(raw: RawPlaylistPreview) -> PlaylistPreview:
    items = tuple(raw.items[:MAX_BATCH_ITEMS])
    return PlaylistPreview(
        playlist_id=raw.playlist_id,
        title=raw.title or "Playlist",
        items=items,
        total=len(raw.items),
    )


def batch_items(items: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Validate the items a Batch was asked to download.

    Ids come back from the browser, so they are re-checked here even though the
    Preview put them there: the form is not a trust boundary.
    """
    cleaned: list[tuple[str, str]] = []
    for video_id, title in items:
        if not _is_video_id(video_id):
            raise InvalidUrl("That playlist contains a video we cannot download.")
        cleaned.append((video_id, (title or video_id).strip()[:200] or video_id))

    if not cleaned:
        raise InvalidUrl("That playlist has no videos.")
    return tuple(cleaned[:MAX_BATCH_ITEMS])


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

    Above 1080p YouTube rarely ships H.264, so the fallback usually picks
    VP9/AV1 while the container stays MP4. The preset is a ceiling, not a
    promise of an exact height or codec.
    """
    if preset.kind == PresetKind.AUDIO:
        return "bestaudio[ext=m4a]/bestaudio/best"

    height = preset.height
    return (
        f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={height}]+bestaudio"
        f"/best[height<={height}]"
    )
