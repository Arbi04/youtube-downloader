import re

import pytest
from fastapi.testclient import TestClient

from fakes import FakeDownloader, ManualExecutor
from youtube_downloader.app import create_app
from youtube_downloader.config import Settings
from youtube_downloader.services.downloader import DownloadError

VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
VIDEO_ID = "dQw4w9WgXcQ"


@pytest.fixture
def client_factory(tmp_path):
    def build(downloader=None, max_concurrent=1, max_queued=1):
        settings = Settings(
            temp_root=tmp_path / "jobs",
            max_concurrent_jobs=max_concurrent,
            max_queued_jobs=max_queued,
        )
        downloader = downloader or FakeDownloader()
        executor = ManualExecutor()
        app = create_app(settings=settings, downloader=downloader, executor=executor)
        return TestClient(app), downloader, executor

    return build


def _start_job(client, preset="mp4-720", max_height="720"):
    response = client.post(
        "/jobs",
        data={"video_id": VIDEO_ID, "preset": preset, "max_height": max_height},
    )
    job_id = re.search(r'hx-get="/jobs/([0-9a-f]+)"', response.text)
    return response, (job_id.group(1) if job_id else None)


def test_index_renders_the_form(client_factory):
    client, _, _ = client_factory()

    response = client.get("/")

    assert response.status_code == 200
    assert 'hx-post="/preview"' in response.text
    assert "Check link" in response.text


def test_static_assets_are_served_locally(client_factory):
    client, _, _ = client_factory()

    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200


def test_preview_renders_metadata_and_presets(client_factory):
    client, _, _ = client_factory()

    response = client.post("/preview", data={"url": VIDEO_URL})

    assert response.status_code == 200
    assert "Test video" in response.text
    assert "3:42" in response.text
    assert "MP4 360p" in response.text
    assert "MP3 192k" in response.text


def test_preview_keeps_taller_presets_visible_but_disabled(client_factory):
    client, _, _ = client_factory()  # fake video tops out at 720p

    response = client.post("/preview", data={"url": VIDEO_URL})

    assert "max 720p" in response.text
    assert 'value="mp4-1080" disabled' in " ".join(response.text.split())


def test_preview_rejects_non_youtube_links(client_factory):
    client, _, _ = client_factory()

    response = client.post("/preview", data={"url": "https://vimeo.com/12345"})

    assert response.status_code == 200
    assert "not a YouTube link" in response.text


def test_preview_surfaces_engine_errors(client_factory):
    class BrokenDownloader(FakeDownloader):
        def preview(self, video_id):
            raise DownloadError("bot_check", "Set YTDLP_COOKIES_FILE and retry.")

    client, _, _ = client_factory(BrokenDownloader())

    response = client.post("/preview", data={"url": VIDEO_URL})

    assert response.status_code == 200
    assert "YTDLP_COOKIES_FILE" in response.text


def test_starting_a_job_renders_a_polling_progress_bar(client_factory):
    client, downloader, _ = client_factory()

    response, job_id = _start_job(client)

    assert response.status_code == 200
    assert job_id is not None
    assert 'hx-trigger="every 1s"' in response.text
    assert "Cancel" in response.text
    assert downloader.calls == []


def test_finished_job_offers_the_file_and_stops_polling(client_factory):
    client, _, executor = client_factory()
    _, job_id = _start_job(client)

    executor.run_next()
    response = client.get(f"/jobs/{job_id}")

    assert "Save file" in response.text
    assert "hx-trigger" not in response.text


def test_preset_above_the_video_height_is_refused(client_factory):
    client, _, _ = client_factory()

    response, job_id = _start_job(client, preset="mp4-1080", max_height="720")

    assert response.status_code == 409
    assert job_id is None
    assert "tops out at 720p" in response.text


def test_queue_full_is_refused(client_factory):
    client, _, _ = client_factory(max_concurrent=1, max_queued=0)

    first, _ = _start_job(client)
    second, _ = _start_job(client)

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Too many downloads" in second.text


def test_cancelling_a_job_shows_the_cancelled_state(client_factory):
    client, downloader, executor = client_factory()
    _, job_id = _start_job(client)

    cancelled = client.post(f"/jobs/{job_id}/cancel")
    executor.run_next()

    assert "Cancelled" in cancelled.text
    assert downloader.calls == []
    assert "Cancelled" in client.get(f"/jobs/{job_id}").text


def test_file_is_served_once_then_removed(client_factory):
    client, _, executor = client_factory()
    _, job_id = _start_job(client)
    executor.run_next()

    response = client.get(f"/jobs/{job_id}/file")

    assert response.status_code == 200
    assert response.content == b"data"
    assert "attachment" in response.headers["content-disposition"]
    assert client.get(f"/jobs/{job_id}/file").status_code == 404


def test_unknown_job_returns_404(client_factory):
    client, _, _ = client_factory()

    assert client.get("/jobs/deadbeef").status_code == 404
