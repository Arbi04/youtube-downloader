import pytest

from youtube_downloader.services.youtube import (
    InvalidUrl,
    PresetKind,
    Preview,
    RawPreview,
    UnknownPreset,
    available_presets,
    build_preview,
    format_selector,
    parse_video_url,
    preset_by_key,
    watch_url,
)

VIDEO_ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "raw",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"http://youtube.com/watch?v={VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}",
        f"https://music.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
        f"youtu.be/{VIDEO_ID}",
        f"www.youtube.com/watch?v={VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}",
        f"https://www.youtube.com/watch?v={VIDEO_ID}&list=PL123&t=42",
        f"  https://youtu.be/{VIDEO_ID}?t=30  ",
    ],
)
def test_accepts_single_video_links(raw):
    assert parse_video_url(raw) == VIDEO_ID


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "    ",
        "not a link",
        "https://vimeo.com/12345",
        "https://example.com/watch?v=dQw4w9WgXcQ",
        f"https://youtube.com.evil.test/watch?v={VIDEO_ID}",
        "javascript:alert(1)",
        "data:text/html,<h1>hi</h1>",
        "file:///etc/passwd",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/",
    ],
)
def test_rejects_anything_else(raw):
    with pytest.raises(InvalidUrl):
        parse_video_url(raw)


def test_watch_url_is_canonical():
    assert watch_url(VIDEO_ID) == f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_video_selector_prefers_h264_then_falls_back():
    selector = format_selector(preset_by_key("mp4-720"))

    assert selector == (
        "bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]"
        "/bestvideo[height<=720]+bestaudio"
        "/best[height<=720]"
    )


def test_audio_selector_ignores_height():
    assert format_selector(preset_by_key("mp3-192")) == "bestaudio[ext=m4a]/bestaudio/best"


def test_preset_extensions():
    assert preset_by_key("mp4-360").extension == "mp4"
    assert preset_by_key("mp3-192").extension == "mp3"
    assert preset_by_key("mp3-192").kind == PresetKind.AUDIO


def test_unknown_preset_is_rejected():
    with pytest.raises(UnknownPreset):
        preset_by_key("mp4-2160")


def _preview(max_height: int | None) -> Preview:
    return Preview(
        video_id=VIDEO_ID,
        title="A video",
        duration_seconds=90,
        thumbnail_url=None,
        max_height=max_height,
    )


def test_short_video_disables_taller_presets_without_hiding_them():
    presets = {preset.key: preset for preset in available_presets(_preview(480))}

    assert presets["mp4-360"].available is True
    assert presets["mp4-720"].available is False
    assert presets["mp4-1080"].available is False
    assert presets["mp3-192"].available is True


def test_full_hd_video_enables_every_preset():
    assert all(preset.available for preset in available_presets(_preview(1080)))


def test_unknown_height_leaves_presets_available():
    assert all(preset.available for preset in available_presets(_preview(None)))


def test_build_preview_keeps_the_tallest_stream():
    raw = RawPreview(
        video_id=VIDEO_ID,
        title="A video",
        duration_seconds=90,
        thumbnail_url="https://i.ytimg.com/vi/x/hq.jpg",
        heights=(144, 720, 360),
    )

    preview = build_preview(raw)

    assert preview.max_height == 720
    assert preview.title == "A video"
    assert preview.thumbnail_url == "https://i.ytimg.com/vi/x/hq.jpg"


def test_build_preview_without_streams_reports_unknown_height():
    raw = RawPreview(
        video_id=VIDEO_ID,
        title="A video",
        duration_seconds=None,
        thumbnail_url=None,
        heights=(),
    )

    assert build_preview(raw).max_height is None
