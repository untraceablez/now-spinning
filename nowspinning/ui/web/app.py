"""FastAPI app serving the now-playing page and its live update stream.

Useful two ways: as the display itself (Chromium in kiosk mode on a Pi with a
desktop), and as a second screen -- open it on a phone from the couch while the
record plays. Both read the same StateStore the pygame renderer does.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nowspinning.artwork import ArtworkCache
from nowspinning.config import Config
from nowspinning.state import NowPlaying, StateStore

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: SSE connections die silently through some proxies; a periodic comment keeps them honest.
KEEPALIVE_SECONDS = 20.0


def state_payload(state: NowPlaying) -> dict[str, Any]:
    """The JSON the browser consumes, with artwork turned into a fetchable URL."""
    payload = state.to_dict()
    payload["artwork"] = (
        f"/api/art/{state.artwork_path.stem}" if state.artwork_path is not None else None
    )
    return payload


def create_app(config: Config, store: StateStore, artwork: ArtworkCache | None = None) -> FastAPI:
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, StreamingResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError(
            "the web display needs the 'web' extra: pip install 'now-spinning[web]'"
        ) from exc

    cache = artwork or ArtworkCache(config.cache_dir)
    app = FastAPI(title="now-spinning", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/now-playing")
    async def now_playing() -> dict[str, Any]:
        return state_payload(store.snapshot())

    @app.get("/api/theme")
    async def theme() -> dict[str, Any]:
        display = config.display
        return {
            "background": display.background,
            "foreground": display.foreground,
            "accent": display.accent,
            "rpm": display.rpm,
        }

    @app.get("/api/art/{key}", include_in_schema=False)
    async def art(key: str) -> FileResponse:
        path = cache.path_for_key(key)
        if path is None:
            raise HTTPException(status_code=404, detail="unknown artwork")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/stream", include_in_schema=False)
    async def stream() -> StreamingResponse:
        return StreamingResponse(
            _events(store),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


async def _events(store: StateStore) -> Any:
    """Yield one SSE message per state change, plus keepalives while nothing happens."""
    subscription = store.subscribe()
    try:
        while True:
            try:
                state = await asyncio.wait_for(subscription.queue.get(), KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if state is None:
                return
            yield f"data: {json.dumps(state_payload(state))}\n\n"
    except asyncio.CancelledError:  # pragma: no cover - client disconnected
        raise
    finally:
        subscription.close()


def build_server(config: Config, store: StateStore, artwork: ArtworkCache | None = None) -> Any:
    """A uvicorn Server the caller can ``await server.serve()`` on its own loop."""
    import uvicorn

    app = create_app(config, store, artwork)
    settings = uvicorn.Config(
        app,
        host=config.web.host,
        port=config.web.port,
        log_level=config.logging.level.lower(),
        access_log=False,
    )
    return uvicorn.Server(settings)
