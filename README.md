# YouTube Downloader

A local web app that turns a YouTube link into a file you can actually play: pick a resolution or an MP3 bitrate, watch the progress bar, save the file. One user, one video at a time, no accounts, no database.

<p align="center">
  <img
    src="docs/screenshot.png"
    alt="The start screen: a cream page with a thick black border card, a URL field, and a yellow Check link button"
    width="720"
  />
</p>

## Why local-only

This runs on `127.0.0.1` by design. A public, anonymous YouTube downloader gets its IP blocked by YouTube within days, pays for everyone else's bandwidth, and carries legal exposure that a portfolio project has no reason to take on. Running it locally keeps the interesting engineering — job handling, progress, format selection — without the parts that would get the domain taken down.

If you ever expose it, put it behind authentication and a rate limit first.

## Quickstart

Requirements: [uv](https://docs.astral.sh/uv/) and `ffmpeg` on `PATH` (needed to merge high-resolution video with audio and to produce MP3s).

```bash
uv sync
uv run youtube-downloader
```

Open <http://127.0.0.1:8000>.

```bash
ffmpeg -version   # must print a version; the app reports a clear error if it is missing
```

## How it works

1. Paste a URL. The server fetches metadata and renders a **Preview**: title, duration, thumbnail, and the presets this video actually supports.
2. Pick a preset. A **Job** starts in the background and the page polls its progress once a second.
3. When the job finishes, **Save file** hands you the file and the server deletes its temporary copy.

| Preset | What you get |
| --- | --- |
| `mp4-360` / `mp4-720` / `mp4-1080` | H.264 + AAC in an MP4, merged with ffmpeg. H.264 is preferred so the file plays in Windows Media Player, QuickTime, phones, and editors; other codecs are used only when H.264 is unavailable. |
| `mp3-192` | MP3 at 192 kbps with ID3 tags and the thumbnail embedded as cover art. |

Presets taller than the source video stay visible but disabled, labelled `max 720p`, so you can see the limit instead of guessing why a button is missing.

## Configuration

All settings are environment variables, read once at startup.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8000` | Port on `127.0.0.1`. |
| `TEMP_ROOT` | `<system temp>/youtube-downloader` | Where in-flight files live. |
| `YTDLP_COOKIES_FILE` | unset | Path to a Netscape-format `cookies.txt`. |
| `MAX_CONCURRENT_JOBS` | `2` | Downloads running at once. |
| `MAX_QUEUED_JOBS` | `5` | Jobs allowed to wait behind them. |
| `JOB_TIMEOUT_SECONDS` | `900` | Cooperative cutoff per job. |

## When YouTube says "Sign in to confirm you're not a bot"

Some videos — and eventually most requests from a datacenter IP — need cookies. yt-dlp reads them from a Netscape-format `cookies.txt`.

1. Log in to YouTube in your browser.
2. Install a cookie-export extension such as **Get cookies.txt LOCALLY** (Chrome, Edge, Firefox). It produces the Netscape format yt-dlp expects and keeps the export local.
3. Open `youtube.com`, click the extension, and export. Save the file as `cookies.txt` outside this repo.
4. Point the app at it and restart:

```powershell
$env:YTDLP_COOKIES_FILE = "C:\path\to\cookies.txt"
uv run youtube-downloader
```

On macOS/Linux the same variable works inline: `YTDLP_COOKIES_FILE=/path/to/cookies.txt uv run youtube-downloader`.

The app maps that failure to a message naming this variable, rather than showing a traceback. The same applies to age-restricted videos.

`cookies.txt` is a credential: it grants access to the account it came from. `.gitignore` excludes `cookies*.txt`, but keep the file outside the project anyway, and never paste it into an issue. Cookies expire after weeks or months — re-export when downloads start failing again.

## Known limitations

- **No playlists or channels** — one video per link, by design.
- **No history and no database** — job state is in memory, so a restart drops whatever was in flight.
- **Files are temporary** — the server deletes its copy after the browser has the file, and sweeps leftovers older than two hours at startup.
- **Not multi-user** — no auth, no per-user quotas.
- **YouTube moves constantly** — when downloads start failing after a YouTube change, upgrade the engine: `uv lock --upgrade-package yt-dlp && uv sync`.

## Tests

```bash
uv run pytest -q
```

The suite is offline: `yt-dlp` is replaced by a fake behind the `Downloader` protocol, jobs run through a manual executor, and time is injected. No network, no sleeps. What the tests *cannot* prove is that a real download works — that stays a manual smoke check, because it depends on YouTube.

## Architecture

```
routes/            HTMX endpoints: preview, job lifecycle, file delivery
services/jobs.py   in-memory registry, bounded queue, cancel, timeout, sweep
services/downloader.py   the only module that imports yt_dlp
services/youtube.py      URL allowlist, Preview, Preset, format selectors
templates/         Jinja2 + HTMX fragments, no build step
static/            hand-written CSS, vendored htmx
```

Two decisions are worth knowing before reading the code:

- **Jobs run in-process.** No Redis, no Celery, no database — see [`docs/adr/0001-in-process-jobs.md`](docs/adr/0001-in-process-jobs.md). The consequence is that cancellation is cooperative: Python cannot kill a thread, so the worker checks a flag on each progress hook.
- **`services/downloader.py` is the only yt-dlp import.** Everything else talks to the `Downloader` protocol. That seam is what keeps the suite offline and what would make swapping engines a local change.

Vocabulary lives in [`GLOSSARY.md`](GLOSSARY.md): **Preview**, **Preset**, **Job**.

## Legal

For personal use with content you have the rights to. Downloading copyrighted material may violate YouTube's Terms of Service and the rights of the uploader. This project is not deployed as a public service, and you should not deploy it as one.
