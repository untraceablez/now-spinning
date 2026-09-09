"""FastAPI app serving the now-playing page and its live update stream.

Useful two ways: as the display itself (Chromium in kiosk mode on a Pi with a
desktop), and as a second screen -- open it on a phone from the couch while the
record plays. Both read the same StateStore the pygame renderer does.

Theme support: the browser loads themes dynamically from /api/themes/{name}/.
Themes are self-contained HTML/CSS/JavaScript applications that consume the
window.nowSpinning API for state updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nowspinning.artwork import ArtworkCache
from nowspinning.config import Config
from nowspinning.fonts import FontLibrary
from nowspinning.state import NowPlaying, StateStore
from nowspinning.ui import geometry
from nowspinning.ui.theme import ThemeLoader

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
    fonts = FontLibrary(config.fonts, config.cache_dir)
    loader = ThemeLoader()
    app = FastAPI(title="now-spinning", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/now-playing")
    async def now_playing() -> dict[str, Any]:
        return state_payload(store.snapshot())

    @app.get("/api/themes", include_in_schema=False)
    async def list_themes() -> dict[str, list[str]]:
        return {"themes": loader.list_themes()}

    @app.get("/api/themes/{theme_name}/config", include_in_schema=False)
    async def theme_config(theme_name: str) -> dict[str, Any]:
        """Theme config including manifest, server geometry, and display settings."""
        theme = loader.get_theme(theme_name)
        if not theme:
            raise HTTPException(status_code=404, detail="unknown theme")

        display = config.display
        return {
            "theme_name": theme.name,
            "manifest": theme.manifest,
            "geometry": geometry.as_dict(),
            "display": display.model_dump(mode="json"),
            "fonts": {
                role: getattr(config.fonts, role).model_dump(mode="json")
                for role in ("heading", "title", "artist", "album")
            },
        }

    @app.get("/api/themes/{theme_name}/index.html", include_in_schema=False)
    async def theme_html(theme_name: str) -> FileResponse:
        theme = loader.get_theme(theme_name)
        if not theme:
            raise HTTPException(status_code=404, detail="unknown theme")
        return FileResponse(theme.main_file, media_type="text/html")

    @app.get("/api/themes/{theme_name}/style.css", include_in_schema=False)
    async def theme_style(theme_name: str) -> FileResponse:
        theme = loader.get_theme(theme_name)
        if not theme:
            raise HTTPException(status_code=404, detail="unknown theme")
        style_path = theme.file_path("style.css")
        if not style_path:
            raise HTTPException(status_code=404, detail="no stylesheet")
        return FileResponse(style_path, media_type="text/css")

    @app.get("/api/themes/{theme_name}/script.js", include_in_schema=False)
    async def theme_script(theme_name: str) -> FileResponse:
        theme = loader.get_theme(theme_name)
        if not theme:
            raise HTTPException(status_code=404, detail="unknown theme")
        script_path = theme.file_path("script.js")
        if not script_path:
            raise HTTPException(status_code=404, detail="no script")
        return FileResponse(script_path, media_type="application/javascript")

    @app.get("/api/themes/{theme_name}/assets/{filename}", include_in_schema=False)
    async def theme_asset(theme_name: str, filename: str) -> FileResponse:
        theme = loader.get_theme(theme_name)
        if not theme:
            raise HTTPException(status_code=404, detail="unknown theme")
        asset_path = theme.asset_path(filename)
        if not asset_path:
            raise HTTPException(status_code=404, detail="unknown asset")
        return FileResponse(asset_path)

    @app.get("/api/asset/{name}", include_in_schema=False)
    async def asset(name: str) -> FileResponse:
        """Legacy: the sleeve artwork components used by themes."""
        path = geometry.ASSETS / Path(name).name
        if path.suffix != ".png" or not path.is_file():
            raise HTTPException(status_code=404, detail="unknown asset")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/font/{role}", include_in_schema=False)
    async def font(role: str) -> FileResponse:
        """The very font file the panel is using, so the two match."""
        if role not in ("heading", "title", "artist", "album"):
            raise HTTPException(status_code=404, detail="unknown role")
        path = await asyncio.to_thread(fonts.resolve, getattr(config.fonts, role))
        if path is None:
            raise HTTPException(status_code=404, detail="no font file for that role")
        return FileResponse(path, media_type="font/ttf")

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
