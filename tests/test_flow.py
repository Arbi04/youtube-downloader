"""End-to-end journey through the real app, wired to fakes."""

import io
import json
import os
import re
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from fakes import PLAYLIST_ID, FakeDownloader, ManualExecutor
from youtube_downloader.app import create_app
from youtube_downloader.config import Settings
from youtube_downloader.services.downloader import DownloadError
from youtube_downloader.services.youtube import RawPreview

VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def build_client(tmp_path, downloader=None, **overrides):
    settings = Settings(
        temp_root=tmp_path / "jobs",
        max_concurrent_jobs=1,
        max_queued_jobs=1,
        **overrides,
    )
    downloader = downloader or FakeDownloader()
    executor = ManualExecutor()
    return TestClient(
        create_app(
            settings=settings,
            downloader=downloader,
            executor=executor,
            sleep=lambda seconds: None,
        )
    ), executor, downloader


def job_id_from(html: str) -> str:
    return re.search(r'hx-get="/jobs/([0-9a-f]+)"', html).group(1)


def test_preview_then_download_then_file_disappears(tmp_path):
    client, executor, _ = build_client(tmp_path)

    preview = client.post("/preview", data={"url": VIDEO_URL})
    assert "Test video" in preview.text

    started = client.post(
        "/jobs",
        data={"video_id": VIDEO_ID, "preset": "mp4-720", "max_height": "720"},
    )
    assert started.status_code == 200
    job_id = job_id_from(started.text)

    executor.run_next()

    finished = client.get(f"/jobs/{job_id}")
    assert "Save file" in finished.text

    saved = client.get(f"/jobs/{job_id}/file")
    assert saved.status_code == 200
    assert saved.content == b"data"

    assert client.get(f"/jobs/{job_id}/file").status_code == 404


def test_audio_preset_produces_an_mp3(tmp_path):
    client, executor, _ = build_client(tmp_path)
    started = client.post(
        "/jobs",
        data={"video_id": VIDEO_ID, "preset": "mp3-192", "max_height": "2160"},
    )
    job_id = job_id_from(started.text)

    executor.run_next()
    saved = client.get(f"/jobs/{job_id}/file")

    assert saved.headers["content-type"] == "audio/mpeg"
    assert ".mp3" in saved.headers["content-disposition"]


def test_unknown_video_height_keeps_every_preset_enabled(tmp_path):
    unknown = FakeDownloader(
        RawPreview(
            video_id=VIDEO_ID,
            title="Mystery",
            duration_seconds=None,
            thumbnail_url=None,
            heights=(),
        )
    )
    client, _, _ = build_client(tmp_path, downloader=unknown)

    response = client.post("/preview", data={"url": VIDEO_URL})

    assert "unknown length" in response.text
    assert "disabled" not in response.text


def test_startup_sweeps_leftovers_from_a_previous_run(tmp_path):
    settings = Settings(temp_root=tmp_path / "jobs")
    stale = settings.temp_root / "abandoned"
    stale.mkdir(parents=True)
    old = time.time() - 3 * 60 * 60
    os.utime(stale, (old, old))

    with TestClient(create_app(settings=settings, downloader=FakeDownloader())):
        pass

    assert not stale.exists()


def test_invalid_url_never_starts_a_job(tmp_path):
    client, _, downloader = build_client(tmp_path)

    response = client.post("/preview", data={"url": "https://example.com/x"})

    assert "not a YouTube link" in response.text
    assert downloader.calls == []

def test_playlist_preview_then_one_archive(tmp_path):
    client, executor, _ = build_client(tmp_path)

    preview = client.post(
        "/preview",
        data={"url": f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"},
    )
    assert "Test playlist" in preview.text

    started = client.post(
        "/batches",
        data={
            "playlist_title": "Test playlist",
            "preset": "mp3-192",
            "items": json.dumps(
                [["aaaaaaaaaaa", "First song"], ["bbbbbbbbbbb", "Second song"]]
            ),
        },
    )
    batch_id = re.search(r'hx-get="/batches/([0-9a-f]+)"', started.text).group(1)

    executor.run_next()

    archive = client.get(f"/batches/{batch_id}/zip")
    assert archive.status_code == 200
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        assert bundle.namelist() == [
            "Test playlist/01 - file.mp3",
            "Test playlist/02 - file.mp3",
        ]

    assert client.get(f"/batches/{batch_id}/zip").status_code == 404


class FailFirstItem(FakeDownloader):
    """Fails one playlist item once, so the first pass needs a manual retry."""

    def __init__(self) -> None:
        super().__init__()
        self.failing = "bbbbbbbbbbb"

    def download(self, video_id, preset, destination, on_progress, should_cancel):
        if video_id == self.failing:
            self.failing = None
            self.calls.append((video_id, preset.key))
            raise DownloadError("private", "This video is private.")
        return super().download(
            video_id, preset, destination, on_progress, should_cancel
        )


def test_playlist_retry_ends_with_one_archive(tmp_path):
    downloader = FailFirstItem()
    client, executor, _ = build_client(tmp_path, downloader=downloader)

    started = client.post(
        "/batches",
        data={
            "playlist_title": "Test playlist",
            "preset": "mp3-192",
            "items": json.dumps(
                [
                    ["aaaaaaaaaaa", "First song"],
                    ["bbbbbbbbbbb", "Second song"],
                    ["ccccccccccc", "Third song"],
                ]
            ),
        },
    )
    batch_id = re.search(r'hx-get="/batches/([0-9a-f]+)"', started.text).group(1)
    executor.run_next()

    first_pass = client.get(f"/batches/{batch_id}")
    assert "2 of 3 saved" in first_pass.text
    assert "Save archive (2 files)" in first_pass.text
    assert "Retry 1 failed item" in first_pass.text

    retried = client.post(f"/batches/{batch_id}/retry")
    assert retried.status_code == 200
    assert "2 of 3 saved" in retried.text
    assert "Earlier archive" not in retried.text

    executor.run_next()

    done = client.get(f"/batches/{batch_id}")
    assert "3 of 3 saved" in done.text
    assert done.text.count("Save archive (3 files)") == 1

    archive = client.get(f"/batches/{batch_id}/zip")
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        assert bundle.namelist() == [
            "Test playlist/01 - file.mp3",
            "Test playlist/02 - file.mp3",
            "Test playlist/03 - file.mp3",
        ]


def test_a_playlist_url_does_not_start_a_single_download(tmp_path):
    client, _, downloader = build_client(tmp_path)

    response = client.post(
        "/preview",
        data={"url": f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"},
    )

    assert downloader.calls == []
    assert 'hx-post="/batches"' in response.text
