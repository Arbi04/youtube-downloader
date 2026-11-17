"""End-to-end journey through the real app, wired to fakes."""

import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from fakes import FakeDownloader, ManualExecutor
from youtube_downloader.app import create_app
from youtube_downloader.config import Settings
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
        create_app(settings=settings, downloader=downloader, executor=executor)
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
