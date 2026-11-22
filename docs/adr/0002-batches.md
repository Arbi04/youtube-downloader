# Batches: one playlist, one worker slot, one archive

A **Batch** downloads every video in a playlist under a single Preset and delivers the results as one ZIP. It is a container for ordinary Jobs, not a new execution model: each item is still a Job with the same states, the same progress hook, and the same cooperative cancellation.

## Decisions

- **A Batch occupies one worker slot and downloads its items one after another.** The pool still holds `MAX_CONCURRENT_JOBS` jobs, so a Batch and a single video can run side by side, but one Batch never fans out. Two parallel batches would mean four concurrent YouTube requests when the pool is 2 — with cookies, that is exactly what triggers bot-checks.
- **The queue limit is unchanged.** Submitting a 25-item playlist costs one slot, not 25. The alternative — one Job per item — would immediately exceed `MAX_CONCURRENT_JOBS + MAX_QUEUED_JOBS` (7) and force a rewrite of [ADR-0001](0001-in-process-jobs.md).
- **One ZIP, built after the last item finishes.** Telegram-style trickle of 25 separate browser downloads is worse than one archive, and per-item links would break the "the server deletes its copy once it has been served" guarantee: serving one item would leave the archive incomplete. Serving a partial archive keeps its files, so a failed item can still be retried; only a complete archive is deleted after serving.
- **A failing item does not stop the Batch.** Private, age-restricted, and geo-blocked videos are common in real playlists, so the Batch records a per-item failure and continues. If *every* item fails, the Batch is `failed` and no archive is offered.
- **Presets are offered unconditionally for playlists.** Deciding availability up front would cost one metadata request per item, making a 25-item Preview slower than the download itself. The format selector already caps height per item.
- **25 items maximum**, truncated from the front. The limit is about temp disk and the user's patience, not about YouTube.
- **Retrying happens inside the same Batch.** Only the failed items go back to `queued`; the item list, the counter, and the archive keep the playlist's original numbering, and the one ZIP is rebuilt from every saved file once the retry finishes. A new Batch would restart `0 of 14 saved` and force the user to download two archives for one playlist. During a retry the progress bar tracks the item being retried, while the summary keeps the playlist-wide count.
- **Transient failures are retried automatically.** A 403, an HTTP 429, or a dropped connection is attempted up to three times with a 2s then 5s pause before the item is marked failed. Permanent failures (private, age-restricted) are not retried. Items are spaced two seconds apart to avoid provoking a rate limit.
- **`/playlist?list=` only.** `watch?v=…&list=…` stays a single video, so a link copied from a video page never silently turns into 25 downloads.

## Consequences

- A Batch can hold a worker slot for many minutes; a second Batch waits even when the other worker is idle. Accepted while `MAX_CONCURRENT_JOBS=2`.
- Temp usage grows with the playlist: 25 × ~30 MB is roughly 750 MB before the ZIP is built. The ZIP is stored, not deflated — media does not compress.
- Cancellation is still cooperative: it stops the item in flight and marks the rest cancelled. Cancelling a fresh Batch discards its files. Cancelling a retry keeps the files saved by the first pass, leaves the interrupted items failed so Retry can be offered again, and rebuilds the archive. Per-item cancellation was not worth the UI.
- A complete archive is deleted after it has been served, like any single file; a partial archive keeps its files so its failed items can still be retried. Abandoning the page leaves either to the two-hour startup sweep.
