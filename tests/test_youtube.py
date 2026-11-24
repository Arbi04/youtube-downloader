import pytest

from youtube_downloader.services.youtube import (
    MAX_BATCH_ITEMS,
    PRESETS,
    InvalidUrl,
    PlaylistTarget,
    PresetKind,
    Preview,
    RawPlaylistItem,
    RawPlaylistPreview,
    RawPreview,
    UnknownPreset,
    VideoTarget,
    available_presets,
    batch_items,
    build_playlist_preview,
    build_preview,
    format_selector,
    parse_target,
    parse_video_url,
    playlist_url,
    preset_by_key,
    watch_url,
)

VIDEO_ID = "dQw4w9WgXcQ"
PLAYLIST_ID = "PLwiyx1dc3P2JR9N8gQaQN_BCvlSlap7re"


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


@pytest.mark.parametrize(
    ("preset_key", "height"),
    [
        ("mp4-360", 360),
        ("mp4-480", 480),
        ("mp4-720", 720),
        ("mp4-1080", 1080),
        ("mp4-1440", 1440),
    ],
)
def test_video_selector_prefers_h264_then_falls_back(preset_key, height):
    selector = format_selector(preset_by_key(preset_key))

    assert selector == (
        f"bestvideo[height<={height}][vcodec^=avc1]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={height}]+bestaudio"
        f"/best[height<={height}]"
    )


@pytest.mark.parametrize("preset_key", ["mp3-128", "mp3-192", "mp3-320"])
def test_audio_selector_ignores_height(preset_key):
    selector = format_selector(preset_by_key(preset_key))

    assert selector == "bestaudio[ext=m4a]/bestaudio/best"


def test_preset_extensions():
    assert preset_by_key("mp4-360").extension == "mp4"
    assert preset_by_key("mp4-480").extension == "mp4"
    assert preset_by_key("mp4-1440").extension == "mp4"
    assert preset_by_key("mp3-128").extension == "mp3"
    assert preset_by_key("mp3-192").extension == "mp3"
    assert preset_by_key("mp3-320").extension == "mp3"
    assert preset_by_key("mp3-192").kind == PresetKind.AUDIO


def test_preset_ladder_pairs_video_heights_with_audio_bitrates():
    assert [preset.key for preset in PRESETS] == [
        "mp4-360",
        "mp4-480",
        "mp4-720",
        "mp4-1080",
        "mp4-1440",
        "mp3-128",
        "mp3-192",
        "mp3-320",
    ]
    assert [
        preset.height for preset in PRESETS if preset.kind == PresetKind.VIDEO
    ] == [360, 480, 720, 1080, 1440]
    assert [
        preset.bitrate for preset in PRESETS if preset.kind == PresetKind.AUDIO
    ] == [128, 192, 320]


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
    assert presets["mp4-480"].available is True
    assert presets["mp4-720"].available is False
    assert presets["mp4-1080"].available is False
    assert presets["mp4-1440"].available is False
    assert presets["mp3-128"].available is True
    assert presets["mp3-192"].available is True
    assert presets["mp3-320"].available is True


def test_full_hd_video_disables_1440p():
    presets = {preset.key: preset for preset in available_presets(_preview(1080))}

    assert presets["mp4-1080"].available is True
    assert presets["mp4-1440"].available is False


def test_1440_video_enables_every_preset():
    assert all(preset.available for preset in available_presets(_preview(1440)))


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

# --- Playlist targets -------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        f"https://www.youtube.com/playlist?list={PLAYLIST_ID}",
        f"http://youtube.com/playlist?list={PLAYLIST_ID}&si=abc123",
        f"https://m.youtube.com/playlist?list={PLAYLIST_ID}",
        "https://music.youtube.com/playlist?list=OLAK5uy_abcdef",
        "www.youtube.com/playlist?list=WL",
    ],
)
def test_playlist_links_become_playlist_targets(raw):
    target = parse_target(raw)

    assert isinstance(target, PlaylistTarget)
    assert target.playlist_id


def test_a_watch_link_with_a_list_parameter_stays_one_video():
    target = parse_target(
        f"https://www.youtube.com/watch?v={VIDEO_ID}&list={PLAYLIST_ID}"
    )

    assert isinstance(target, VideoTarget)
    assert target.video_id == VIDEO_ID


@pytest.mark.parametrize(
    "raw",
    [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
    ],
)
def test_video_links_become_video_targets(raw):
    assert isinstance(parse_target(raw), VideoTarget)


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.youtube.com/playlist",
        "https://www.youtube.com/playlist?list=",
        "https://www.youtube.com/playlist?list=has%20a%20space",
        "https://vimeo.com/playlist?list=PL1234567890",
    ],
)
def test_broken_playlist_links_are_rejected(raw):
    with pytest.raises(InvalidUrl):
        parse_target(raw)


def test_a_playlist_link_is_not_a_single_video():
    with pytest.raises(InvalidUrl):
        parse_video_url(f"https://www.youtube.com/playlist?list={PLAYLIST_ID}")


def test_playlist_url_is_canonical():
    assert playlist_url(PLAYLIST_ID) == (
        f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
    )


def _raw_playlist(count: int, title: str = "A list") -> RawPlaylistPreview:
    return RawPlaylistPreview(
        playlist_id=PLAYLIST_ID,
        title=title,
        items=tuple(
            RawPlaylistItem(f"{index:011d}", f"Song {index}", index)
            for index in range(count)
        ),
    )


def test_playlist_preview_keeps_order_and_counts_the_rest():
    preview = build_playlist_preview(_raw_playlist(40))

    assert len(preview.items) == MAX_BATCH_ITEMS
    assert preview.total == 40
    assert preview.truncated is True
    assert [item.title for item in preview.items[:2]] == ["Song 0", "Song 1"]


def test_short_playlist_is_not_marked_truncated():
    preview = build_playlist_preview(_raw_playlist(3))

    assert preview.truncated is False
    assert len(preview.items) == 3


def test_playlist_without_a_title_falls_back():
    assert build_playlist_preview(_raw_playlist(1, title="")).title == "Playlist"


def test_batch_items_rejects_a_malformed_video_id():
    with pytest.raises(InvalidUrl):
        batch_items([("too-short", "A song")])


def test_batch_items_rejects_an_empty_playlist():
    with pytest.raises(InvalidUrl):
        batch_items([])


def test_batch_items_strips_titles_and_caps_the_count():
    items = batch_items([(f"{index:011d}", "   padded   ") for index in range(30)])

    assert len(items) == MAX_BATCH_ITEMS
    assert items[0] == ("00000000000", "padded")


def test_batch_items_falls_back_to_the_id_for_a_blank_title():
    assert batch_items([(f"{1:011d}", "   ")])[0][1] == f"{1:011d}"
