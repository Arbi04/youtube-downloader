"""HTMX endpoints: Preview, Job and Batch lifecycle, and file delivery."""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from youtube_downloader.services.downloader import DownloadError
from youtube_downloader.services.jobs import (
    Batch,
    Job,
    QueueFull,
    RetryUnavailable,
)
from youtube_downloader.services.youtube import (
    PRESETS,
    InvalidUrl,
    PlaylistTarget,
    UnknownPreset,
    available_presets,
    batch_items,
    build_playlist_preview,
    build_preview,
    parse_target,
    preset_by_key,
)

router = APIRouter()


def _error(request: Request, message: str, status_code: int = 200) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/error.html",
        {"message": message},
        status_code=status_code,
    )


def _render_job(request: Request, job: Job) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, "partials/progress.html", {"job": job}
    )


def _render_batch(
    request: Request,
    batch: Batch,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/batch.html",
        {"batch": batch, "error": error},
        status_code=status_code,
    )


def _playlist_partial(request: Request, target: PlaylistTarget) -> HTMLResponse:
    raw = request.app.state.downloader.playlist_preview(target.playlist_id)
    shape = build_playlist_preview(raw)
    if not shape.items:
        return _error(request, "That playlist has no videos we can download.")

    return request.app.state.templates.TemplateResponse(
        request,
        "partials/playlist_preview.html",
        {
            "preview": shape,
            "presets": PRESETS,
            "items": [[item.video_id, item.title] for item in shape.items],
        },
    )


@router.post("/preview", response_class=HTMLResponse)
def preview(request: Request, url: str = Form("")) -> HTMLResponse:
    try:
        target = parse_target(url)
        if isinstance(target, PlaylistTarget):
            return _playlist_partial(request, target)
        raw = request.app.state.downloader.preview(target.video_id)
    except InvalidUrl as exc:
        return _error(request, str(exc))
    except DownloadError as exc:
        return _error(request, exc.message)

    shape = build_preview(raw)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/preview.html",
        {"preview": shape, "presets": available_presets(shape)},
    )


@router.post("/jobs", response_class=HTMLResponse)
def start_job(
    request: Request,
    video_id: str = Form(...),
    preset: str = Form(...),
    max_height: str = Form(""),
) -> HTMLResponse:
    try:
        chosen = preset_by_key(preset)
    except UnknownPreset:
        return _error(request, "That format is not available.", status_code=400)

    # The UI sends the height it saw in the Preview. This is not a security
    # boundary — the format selector already caps height — it only keeps the
    # button honest if the page went stale.
    limit = int(max_height) if max_height.strip().isdigit() else None
    if chosen.height and limit is not None and chosen.height > limit:
        return _error(
            request, f"That video tops out at {limit}p.", status_code=409
        )

    try:
        job = request.app.state.registry.submit(video_id, chosen)
    except QueueFull as exc:
        return _error(request, str(exc), status_code=429)

    return _render_job(request, job)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: str) -> HTMLResponse:
    job = request.app.state.registry.get(job_id)
    if job is None:
        return _error(request, "That download is gone.", status_code=404)
    return _render_job(request, job)


@router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse)
def cancel_job(request: Request, job_id: str) -> HTMLResponse:
    job = request.app.state.registry.cancel(job_id)
    if job is None:
        return _error(request, "That download is gone.", status_code=404)
    return _render_job(request, job)


@router.get("/jobs/{job_id}/file")
def job_file(request: Request, job_id: str) -> Response:
    registry = request.app.state.registry
    job = registry.get(job_id)
    if job is None or job.file_path is None or not job.file_path.exists():
        return HTMLResponse("That file is no longer available.", status_code=404)

    media_type = "audio/mpeg" if job.preset.extension == "mp3" else "video/mp4"
    return FileResponse(
        job.file_path,
        media_type=media_type,
        filename=job.file_path.name,
        background=BackgroundTask(registry.discard_files, job),
    )


def _parse_items(raw: str) -> list[tuple[str, str]]:
    """Read the item pairs the Preview form posted back.

    The form is not a trust boundary, so this only checks shape; ``batch_items``
    re-validates every video id.
    """
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise TypeError("items must be a list")

    pairs: list[tuple[str, str]] = []
    for entry in payload:
        if not isinstance(entry, list) or len(entry) != 2:
            raise TypeError("each item must be a [video_id, title] pair")
        video_id, title = entry
        if not isinstance(video_id, str) or not isinstance(title, str):
            raise TypeError("each item must be a [video_id, title] pair")
        pairs.append((video_id, title))
    return pairs


@router.post("/batches", response_class=HTMLResponse)
def start_batch(
    request: Request,
    playlist_title: str = Form("Playlist"),
    preset: str = Form(...),
    items: str = Form("[]"),
) -> HTMLResponse:
    try:
        chosen = preset_by_key(preset)
    except UnknownPreset:
        return _error(request, "That format is not available.", status_code=400)

    try:
        wanted = batch_items(_parse_items(items))
    except (TypeError, ValueError) as exc:
        if isinstance(exc, InvalidUrl):
            return _error(request, str(exc))
        return _error(
            request,
            "This playlist form is out of date. Check the link again to rebuild it.",
        )

    try:
        batch = request.app.state.registry.submit_batch(
            playlist_title, wanted, chosen
        )
    except QueueFull as exc:
        return _error(request, str(exc), status_code=429)

    return _render_batch(request, batch)


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_status(request: Request, batch_id: str) -> HTMLResponse:
    batch = request.app.state.registry.get_batch(batch_id)
    if batch is None:
        return _error(request, "That batch is gone.", status_code=404)
    return _render_batch(request, batch)


@router.post("/batches/{batch_id}/retry", response_class=HTMLResponse)
def retry_batch(request: Request, batch_id: str) -> HTMLResponse:
    """Re-run the failed items inside the same Batch.

    The playlist keeps its items, its counter, and its archive, so retrying two
    videos does not restart ``0 of 14 saved`` or produce a second ZIP. A 403 is
    often transient; a permanent failure is still reported per item.
    """
    registry = request.app.state.registry
    try:
        batch = registry.retry_batch(batch_id)
    except RetryUnavailable as exc:
        current = registry.get_batch(batch_id)
        if current is None:
            return _error(request, "That batch is gone.", status_code=404)
        return _render_batch(request, current, error=str(exc), status_code=409)
    except QueueFull as exc:
        current = registry.get_batch(batch_id)
        if current is None:
            return _error(request, "That batch is gone.", status_code=404)
        return _render_batch(request, current, error=str(exc), status_code=429)

    if batch is None:
        return _error(request, "That batch is gone.", status_code=404)
    return _render_batch(request, batch)


@router.post("/batches/{batch_id}/cancel", response_class=HTMLResponse)
def cancel_batch(request: Request, batch_id: str) -> HTMLResponse:
    batch = request.app.state.registry.cancel_batch(batch_id)
    if batch is None:
        return _error(request, "That batch is gone.", status_code=404)
    return _render_batch(request, batch)


@router.get("/batches/{batch_id}/zip")
def batch_zip(request: Request, batch_id: str) -> Response:
    registry = request.app.state.registry
    batch = registry.get_batch(batch_id)
    if batch is None or batch.zip_path is None or not batch.zip_path.exists():
        return HTMLResponse("That archive is no longer available.", status_code=404)

    # A partial archive may be downloaded before the failed items are retried.
    # Keep the saved files in that case; only a complete batch is discarded.
    background = None
    if batch.finished and not batch.failed:
        background = BackgroundTask(registry.discard_batch, batch)

    return FileResponse(
        batch.zip_path,
        media_type="application/zip",
        filename=batch.zip_path.name,
        background=background,
    )

