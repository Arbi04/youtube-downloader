"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from youtube_downloader.config import Settings
from youtube_downloader.routes import downloads, pages
from youtube_downloader.services.downloader import Downloader, YtDlpDownloader
from youtube_downloader.services.jobs import JobRegistry

PACKAGE_ROOT = Path(__file__).parent
TEMPLATES_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"


def _human_speed(value: float | None) -> str:
    if not value:
        return ""
    speed = float(value)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if speed < 1024:
            return f"{speed:.1f} {unit}"
        speed /= 1024
    return f"{speed:.1f} TB/s"


def _human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown length"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _human_eta(seconds: int | None) -> str:
    if not seconds:
        return ""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def create_app(
    settings: Settings | None = None,
    downloader: Downloader | None = None,
    executor=None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    downloader = downloader or YtDlpDownloader(settings)
    registry = JobRegistry(downloader, settings, executor=executor)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        settings.temp_root.mkdir(parents=True, exist_ok=True)
        registry.sweep()
        yield

    app = FastAPI(title="YouTube Downloader", lifespan=lifespan)
    app.state.settings = settings
    app.state.downloader = downloader
    app.state.registry = registry

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["speed"] = _human_speed
    templates.env.filters["duration"] = _human_duration
    templates.env.filters["eta"] = _human_eta
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(pages.router)
    app.include_router(downloads.router)

    return app
