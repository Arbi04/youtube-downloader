# YouTube Downloader

A single-user, localhost-first web app that turns a YouTube video URL into a downloadable media file.

## Language

**Preview**:
The metadata shown for a video before the user commits to a download: title, duration, thumbnail, and which presets are available.
_Avoid_: metadata, info, lookup, details

**Preset**:
A user-facing download target — a resolution for MP4 or a bitrate for MP3 — mapped to an internal format selector.
_Avoid_: format, quality, option

**Job**:
One download attempt, from the moment the user picks a preset until the file has been served or the attempt has ended in failure or cancellation.
_Avoid_: task, download, request
