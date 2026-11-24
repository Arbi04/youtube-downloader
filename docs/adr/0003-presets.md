# Presets: a short ladder, honest ceilings

A **Preset** is the user-facing download target: MP4 360p, 480p, 720p, 1080p, or 1440p, or MP3 at 128, 192, or 320 kbps. It is not a yt-dlp format string; `services/youtube.py` translates it into one.

## Decisions

- **A height is a ceiling, not a promise.** The selector asks for `height<=N`, so a 360p preset can deliver 240p when 360p is missing. The Preview disables presets taller than the source instead of hiding them, and the note says `max Np` so the limit is visible.
- **H.264 + AAC first, then any codec, then a progressive stream.** Up to 1080p this usually produces a file that plays in Windows Media Player, QuickTime, phones, and editors. Above 1080p YouTube rarely ships H.264, so 1440p is usually VP9/AV1 in an MP4. We do not transcode to H.264: it is slow, CPU-heavy, and loses quality. The 1440p preset carries a warning instead.
- **1440p is the top of the ladder.** 2160p is not offered. A 25-item 4K batch can need tens of GB, and it inherits the same codec caveat. Adding it needs a disk guard first, not just another tuple.
- **MP3 bitrate is a size choice, not a quality ladder.** YouTube's source audio is usually AAC around 128 kbps or Opus around 160 kbps. MP3 320 cannot add detail; it only makes the file larger. 128 exists for smaller files, 320 for players or settings that expect it.
- **One preset per Batch.** A per-item picker would break the one-ZIP flow and add UI the queue does not need. The playlist form warns that high resolutions make much larger batches.
