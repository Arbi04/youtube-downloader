# YouTube Downloader

A single-user, localhost-first web app that turns a YouTube video or playlist URL into downloadable media files.

## Language

**Preview**:
The metadata shown before the user commits to a download. For a video: title, duration, thumbnail, and which presets are available. For a playlist: title, item count, and the items it contains.
_Avoid_: metadata, info, lookup, details

**Preset**:
A user-facing download target — a resolution for MP4 or a bitrate for MP3 — mapped to an internal format selector.
_Avoid_: format, quality, option

**Job**:
One download attempt, from the moment the user picks a preset until the file has been served or the attempt has ended in failure or cancellation.
_Avoid_: task, download, request

**Batch**:
A user action that downloads every video in a playlist under one preset and delivers them as a single archive. Its items are Jobs that share one worker slot and run one after another.
_Avoid_: playlist job, bulk download, queue, task

**Playlist**:
A YouTube URL that names a list of videos rather than a single video. Its Preview lists the items; starting a Batch downloads them. A channel is not a playlist.
_Avoid_: album, collection, channel
