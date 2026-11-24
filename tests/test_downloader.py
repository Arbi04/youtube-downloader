from pathlib import Path

import pytest

from youtube_downloader.config import Settings
from youtube_downloader.services import downloader as downloader_module
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
        ("ERROR: [youtube] 00000000000: This video is unavailable", "unavailable"),
        ("ERROR: ffmpeg not found. Please install or provide the path", "ffmpeg_missing"),
        ("ERROR: Requested format is not available. Use --list-formats", "format_unavailable"),
        (
            "ERROR: \x1b[0;31mERROR:\x1b[0m unable to download video data: "
            "HTTP Error 403: Forbidden",
            "forbidden",
        ),
        ("ERROR: HTTP Error 429: Too Many Requests", "rate_limited"),
        ("ERROR: Unable to download webpage: HTTP Error 503: Service Unavailable", "network"),
        ("ERROR: [Errno 104] Connection reset by peer", "network"),
        ("ERROR: The read operation timed out", "network"),
        ("ERROR: something exploded", "unknown"),
    ],
)
def test_map_error_translates_engine_failures(text, code):
    error = map_error(RuntimeError(text))

    assert error.code == code
    assert error.message


def test_map_error_strips_yt_dlp_colour_codes():
    error = map_error(
        RuntimeError("\x1b[0;31mERROR:\x1b[0m unable to download video data: HTTP Error 403")
    )

    assert "\x1b[" not in error.message
    assert "[0;31m" not in error.message
    assert "403" in error.message
    assert "try this video again" in error.message


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


def _stream(format_id, downloaded, total, extension="mp4", speed=1.0):
    """The shape yt-dlp reports while writing one stream of a merged download."""
    return {
        "status": "downloading",
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "speed": speed,
        "eta": 5,
        "filename": f"C:/tmp/Video [dQw4w9WgXcQ] mp4-720.f{format_id}.{extension}",
    }


def test_merged_download_gives_each_stream_its_own_slice_of_the_bar(downloader):
    seen: list[Progress] = []
    hook = downloader.make_progress_hook(seen.append, lambda: False, streams=2)

    hook(_stream("137", 500, 1000))  # half of the video stream
    hook({"status": "finished"})
    hook(_stream("140", 250, 1000, extension="m4a"))  # a quarter of the audio

    assert [progress.percent for progress in seen] == [25.0, 50.0, 62.5]


def test_progress_never_goes_backwards_across_streams(downloader):
    seen: list[Progress] = []
    hook = downloader.make_progress_hook(seen.append, lambda: False, streams=2)

    for downloaded in (0, 500, 1000):
        hook(_stream("137", downloaded, 1000))
    hook({"status": "finished"})
    for downloaded in (0, 250, 1000):
        hook(_stream("140", downloaded, 1000, extension="m4a"))
    hook({"status": "finished"})

    percents = [progress.percent for progress in seen]
    assert percents == sorted(percents)
    assert percents[0] == 0.0
    assert percents[-1] == 99.0


def test_a_progressive_fallback_uses_the_whole_bar(downloader):
    seen: list[Progress] = []
    hook = downloader.make_progress_hook(seen.append, lambda: False, streams=2)

    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 250,
            "total_bytes": 1000,
            "filename": "C:/tmp/Video [dQw4w9WgXcQ] mp4-720.mp4",
        }
    )

    assert seen[0].percent == 25.0


def test_a_full_stream_still_reads_as_incomplete_until_the_job_is_done(downloader):
    seen: list[Progress] = []
    hook = downloader.make_progress_hook(seen.append, lambda: False)

    hook(_stream("140", 1000, 1000, extension="m4a"))
    hook({"status": "finished"})

    assert [progress.percent for progress in seen] == [99.0, 99.0]


@pytest.mark.parametrize(
    ("preset_key", "expected"),
    [("mp4-720", 2), ("mp4-1440", 2), ("mp3-192", 1)],
)
def test_presets_declare_how_many_streams_they_download(
    downloader, monkeypatch, tmp_path, preset_key, expected
):
    preset = preset_by_key(preset_key)
    destination = tmp_path / "out"
    destination.mkdir()
    produced = destination / f"Video [dQw4w9WgXcQ] {preset_key}.{preset.extension}"
    produced.write_bytes(b"data")

    class FakeYdl:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, _url, download=False):
            return {"requested_downloads": [{"filepath": str(produced)}]}

    captured: dict[str, int] = {}
    real = YtDlpDownloader.make_progress_hook

    def spy(self, on_progress, should_cancel, streams=1):
        captured["streams"] = streams
        return real(self, on_progress, should_cancel, streams)

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FakeYdl)
    monkeypatch.setattr(YtDlpDownloader, "make_progress_hook", spy)

    downloader.download(
        "dQw4w9WgXcQ", preset, destination, lambda _progress: None, lambda: False
    )

    assert captured["streams"] == expected


@pytest.mark.parametrize("preset_key", ["mp3-128", "mp3-192", "mp3-320"])
def test_audio_preset_passes_its_bitrate_to_ffmpeg(
    downloader, monkeypatch, tmp_path, preset_key
):
    preset = preset_by_key(preset_key)
    destination = tmp_path / "out"
    destination.mkdir()
    produced = destination / f"Video [dQw4w9WgXcQ] {preset_key}.{preset.extension}"
    produced.write_bytes(b"data")
    captured: dict = {}

    class FakeYdl:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, _url, download=False):
            return {"requested_downloads": [{"filepath": str(produced)}]}

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FakeYdl)

    downloader.download(
        "dQw4w9WgXcQ", preset, destination, lambda _progress: None, lambda: False
    )

    assert captured["postprocessors"][0] == {
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": str(preset.bitrate),
    }


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


def test_uses_the_first_available_js_runtime(downloader, monkeypatch):
    monkeypatch.setattr(
        downloader_module.shutil,
        "which",
        lambda name: "C:/node/node.exe" if name == "node" else None,
    )

    assert downloader._base_options()["js_runtimes"] == {"node": {}}


def test_prefers_deno_when_it_is_installed(downloader, monkeypatch):
    monkeypatch.setattr(
        downloader_module.shutil, "which", lambda name: f"/usr/bin/{name}"
    )

    assert downloader._base_options()["js_runtimes"] == {"deno": {}}


def test_omits_js_runtime_when_none_is_installed(downloader, monkeypatch):
    monkeypatch.setattr(downloader_module.shutil, "which", lambda name: None)

    assert "js_runtimes" not in downloader._base_options()


def test_yt_dlp_is_imported_only_by_the_downloader():
    root = Path(__file__).resolve().parents[1] / "src" / "youtube_downloader"
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "downloader.py"
        and "import yt_dlp" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []

def test_playlist_preview_reads_flat_entries(downloader, monkeypatch):
    captured: dict = {}

    class FakeYdl:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, url, download=False):
            captured["url"] = url
            captured["download"] = download
            return {
                "title": "My list",
                "entries": [
                    {"id": "aaaaaaaaaaa", "title": "One", "duration": 61},
                    None,
                    {"id": "bbbbbbbbbbb", "title": None, "duration": None},
                ],
            }

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", FakeYdl)

    raw = downloader.playlist_preview("PL1234567890")

    assert captured["url"] == "https://www.youtube.com/playlist?list=PL1234567890"
    assert captured["download"] is False
    assert captured["extract_flat"] == "in_playlist"
    assert captured["noplaylist"] is False
    assert raw.title == "My list"
    assert [item.video_id for item in raw.items] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert raw.items[0].duration_seconds == 61
    assert raw.items[1].title == "bbbbbbbbbbb"


def test_playlist_preview_translates_engine_failures(downloader, monkeypatch):
    class BrokenYdl:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, _url, download=False):
            raise RuntimeError("ERROR: Private playlist")

    monkeypatch.setattr(downloader_module.yt_dlp, "YoutubeDL", BrokenYdl)

    with pytest.raises(DownloadError):
        downloader.playlist_preview("PL1234567890")
