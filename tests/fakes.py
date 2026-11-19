"""Test doubles shared by the offline suite."""

from __future__ import annotations

from youtube_downloader.services.downloader import Progress
from youtube_downloader.services.youtube import (
    RawPlaylistItem,
    RawPlaylistPreview,
    RawPreview,
)

DEFAULT_VIDEO_ID = "dQw4w9WgXcQ"
PLAYLIST_ID = "PLwiyx1dc3P2JR9N8gQaQN_BCvlSlap7re"


class ManualExecutor:
    """Runs submitted work only when the test says so — no threads, no sleeps."""

    def __init__(self) -> None:
        self.pending: list[tuple] = []

    def submit(self, fn, *args):
        self.pending.append((fn, args))

    def run_next(self) -> None:
        fn, args = self.pending.pop(0)
        fn(*args)


class FakeDownloader:
    def __init__(
        self,
        raw_preview: RawPreview | None = None,
        raw_playlist: RawPlaylistPreview | None = None,
        behaviour=None,
    ) -> None:
        self.raw_preview = raw_preview or RawPreview(
            video_id=DEFAULT_VIDEO_ID,
            title="Test video",
            duration_seconds=222,
            thumbnail_url="https://i.ytimg.com/vi/dQw4w9WgXcQ/hq.jpg",
            heights=(360, 720),
        )
        self.raw_playlist = raw_playlist or RawPlaylistPreview(
            playlist_id=PLAYLIST_ID,
            title="Test playlist",
            items=(
                RawPlaylistItem("aaaaaaaaaaa", "First song", 61),
                RawPlaylistItem("bbbbbbbbbbb", "Second song", 122),
                RawPlaylistItem("ccccccccccc", "Third song", 183),
            ),
        )
        self.calls: list[tuple[str, str]] = []
        self.playlist_calls: list[str] = []
        self.behaviour = behaviour

    def preview(self, video_id: str) -> RawPreview:
        return self.raw_preview

    def playlist_preview(self, playlist_id: str) -> RawPlaylistPreview:
        self.playlist_calls.append(playlist_id)
        return self.raw_playlist

    def download(self, video_id, preset, destination, on_progress, should_cancel):
        self.calls.append((video_id, preset.key))
        on_progress(Progress(percent=50.0, speed=42.0, eta=3))
        if self.behaviour is not None:
            return self.behaviour(preset, destination, should_cancel)

        destination.mkdir(parents=True, exist_ok=True)
        path = destination / f"file.{preset.extension}"
        path.write_bytes(b"data")
        return path
