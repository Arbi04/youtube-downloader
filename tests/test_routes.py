import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from fakes import PLAYLIST_ID, FakeDownloader, ManualExecutor
from youtube_downloader.app import create_app
from youtube_downloader.config import Settings
from youtube_downloader.services.downloader import DownloadError

VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
VIDEO_ID = "dQw4w9WgXcQ"
PLAYLIST_URL = f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"

BATCH_ITEMS = [
    ["aaaaaaaaaaa", "First song"],
    ["bbbbbbbbbbb", "Second song"],
    ["ccccccccccc", "Third song"],
]


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

# --- Playlists and batches -------------------------------------------------


def _start_batch(client, preset="mp3-192"):
    response = client.post(
        "/batches",
        data={
            "playlist_title": "Test playlist",
            "preset": preset,
            "items": json.dumps(BATCH_ITEMS),
        },
    )
    match = re.search(r'hx-get="/batches/([0-9a-f]+)"', response.text)
    return response, (match.group(1) if match else None)


def test_playlist_preview_lists_items_and_offers_every_preset(client_factory):
    client, downloader, _ = client_factory()

    response = client.post("/preview", data={"url": PLAYLIST_URL})

    assert response.status_code == 200
    assert "Test playlist" in response.text
    assert "3 videos" in response.text
    assert "First song" in response.text
    assert "Third song" in response.text
    assert downloader.playlist_calls == [PLAYLIST_ID]
    assert "disabled" not in response.text


def test_starting_a_batch_renders_a_polling_checklist(client_factory):
    client, downloader, _ = client_factory()

    response, batch_id = _start_batch(client)

    assert response.status_code == 200
    assert batch_id is not None
    assert 'hx-trigger="every 1s"' in response.text
    assert "First song" in response.text
    assert "Cancel" in response.text
    assert downloader.calls == []


def test_finished_batch_offers_the_archive_and_stops_polling(client_factory):
    client, _, executor = client_factory()
    _, batch_id = _start_batch(client)

    executor.run_next()
    response = client.get(f"/batches/{batch_id}")

    assert "Save archive (3 files)" in response.text
    assert "hx-trigger" not in response.text


def test_archive_is_a_zip_with_the_items_in_order(client_factory):
    client, _, executor = client_factory()
    _, batch_id = _start_batch(client)
    executor.run_next()

    response = client.get(f"/batches/{batch_id}/zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        assert bundle.namelist() == [
            "Test playlist/01 - file.mp3",
            "Test playlist/02 - file.mp3",
            "Test playlist/03 - file.mp3",
        ]


def test_archive_is_served_once_then_removed(client_factory):
    client, _, executor = client_factory()
    _, batch_id = _start_batch(client)
    executor.run_next()

    assert client.get(f"/batches/{batch_id}/zip").status_code == 200
    assert client.get(f"/batches/{batch_id}/zip").status_code == 404


def test_a_full_queue_refuses_a_batch(client_factory):
    client, _, _ = client_factory(max_concurrent=1, max_queued=0)

    first, _ = _start_batch(client)
    second, _ = _start_batch(client)

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Too many downloads" in second.text


def test_a_batch_with_a_bad_video_id_is_refused(client_factory):
    client, downloader, _ = client_factory()

    response = client.post(
        "/batches",
        data={
            "playlist_title": "Test playlist",
            "preset": "mp3-192",
            "items": json.dumps([["short", "A song"]]),
        },
    )

    assert response.status_code == 200
    assert "no longer valid" in response.text
    assert downloader.calls == []


def test_a_batch_with_unreadable_items_is_refused(client_factory):
    client, _, _ = client_factory()

    response = client.post(
        "/batches",
        data={"playlist_title": "x", "preset": "mp3-192", "items": "not json"},
    )

    assert response.status_code == 200
    assert "no longer valid" in response.text


def test_cancelling_a_batch_shows_the_cancelled_state(client_factory):
    client, downloader, executor = client_factory()
    _, batch_id = _start_batch(client)

    cancelled = client.post(f"/batches/{batch_id}/cancel")
    executor.run_next()

    assert "Cancelled" in cancelled.text
    assert downloader.calls == []
    assert "Cancelled" in client.get(f"/batches/{batch_id}").text


def test_unknown_batch_returns_404(client_factory):
    client, _, _ = client_factory()

    assert client.get("/batches/deadbeef").status_code == 404
