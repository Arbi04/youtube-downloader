"""Entry point for ``uv run youtube-downloader``."""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from youtube_downloader.app import create_app
    from youtube_downloader.config import Settings

    settings = Settings.from_env()
    app = create_app(settings)
    uvicorn.run(app, host="127.0.0.1", port=settings.port)
