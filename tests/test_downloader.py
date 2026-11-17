from pathlib import Path

import pytest

from youtube_downloader.config import Settings
from youtube_downloader.services.downloader import (
    DownloadError,
    JobCancelled,
    Progress,
    YtDlpDownloader,
    map_error,
)
from youtube_downloader.services.youtube import preset_by_key


@pytest.fixture
def downloader():
    return YtDlpDownloader(Settings())


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("ERROR: Sign in to confirm you're not a bot. Use --cookies", "bot_check"),
        ("ERROR: This video is age-restricted", "age_restricted"),
        ("ERROR: Private video. Sign in if you've been granted access", "private"),
        ("ERROR: The uploader has not made this video available in your country", "geo_blocked"),
        ("ERROR: Video unavailable", "unavailable"),
        ("ERROR: ffmpeg not found. Please install or provide the path", "ffmpeg_missing"),
        ("ERROR: something exploded", "unknown"),
    ],
)
def test_map_error_translates_engine_failures(text, code):
    error = map_error(RuntimeError(text))

    assert error.code == code
    assert error.message


def test_map_error_tells_the_user_how_to_fix_a_bot_check():
    error = map_error(RuntimeError("Sign in to confirm you're not a bot"))

    assert "YTDLP_COOKIES_FILE" in error.message


def test_map_error_passes_download_errors_through():
    original = DownloadError("private", "This video is private.")

    assert map_error(original) is original


def test_map_error_survives_an_empty_message():
    error = map_error(RuntimeError(""))

    assert error.code == "unknown"
    assert "RuntimeError" in error.message


def test_progress_hook_reports_percentage_speed_and_eta(downloader):
    seen: list[Progress] = []
    hook = downloader.make_progress_hook(seen.append, lambda: False)

    hook(
        {
            "status": "downloading",
            "total_bytes": 1000,
            "downloaded_bytes": 250,
            "speed": 1_500_000.0,
            "eta": 12,
        }
    )

    assert seen == [Progress(percent=25.0, speed=1_500_000.0, eta=12)]


def test_progress_hook_handles_an_unknown_total(downloader):
    seen: list[Progress] = []
    hook = downloader.make_progress_hook(seen.append, lambda: False)

    hook({"status": "downloading", "downloaded_bytes": 500})

    assert seen[0].percent == 0.0


def test_progress_hook_parks_at_99_while_postprocessing(downloader):
    seen: list[Progress] = []
    hook = downloader.make_progress_hook(seen.append, lambda: False)

    hook({"status": "finished"})

    assert seen[0].percent == 99.0


def test_progress_hook_raises_when_cancelled(downloader):
    hook = downloader.make_progress_hook(lambda _progress: None, lambda: True)

    with pytest.raises(JobCancelled):
        hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})


def test_cookies_are_only_passed_when_configured(downloader, tmp_path):
    assert "cookiefile" not in downloader._base_options()

    cookies = tmp_path / "cookies.txt"
    configured = YtDlpDownloader(Settings(cookies_file=cookies))

    assert configured._base_options()["cookiefile"] == str(cookies)


def test_output_template_names_the_preset_and_caps_the_title(downloader):
    template = downloader.output_template(preset_by_key("mp4-720"))

    assert template == "%(title).120B [%(id)s] mp4-720.%(ext)s"


def test_yt_dlp_is_imported_only_by_the_downloader():
    root = Path(__file__).resolve().parents[1] / "src" / "youtube_downloader"
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "downloader.py"
        and "import yt_dlp" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
