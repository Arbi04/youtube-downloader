"""HTMX endpoints: Preview, Job lifecycle, and file delivery."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from youtube_downloader.services.downloader import DownloadError
from youtube_downloader.services.jobs import Job, QueueFull
from youtube_downloader.services.youtube import (
    InvalidUrl,
    UnknownPreset,
    available_presets,
    build_preview,
    parse_video_url,
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


@router.post("/preview", response_class=HTMLResponse)
def preview(request: Request, url: str = Form("")) -> HTMLResponse:
    try:
        video_id = parse_video_url(url)
        raw = request.app.state.downloader.preview(video_id)
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
