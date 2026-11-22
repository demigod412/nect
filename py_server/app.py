"""
Application entrypoint for Nectra Platform (Python).

FastAPI app with lifespan-managed background tasks, static frontend serving,
and explicit CORS. Credentials are enabled, so origins are enumerated rather
than wildcarded — browsers reject `*` combined with `allow_credentials`.
"""
import os
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

from database import init_db, get_setting
import market
import engine
import drainer_worker
from routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("nectra")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")


def _origins() -> list:
    out = [o.strip().rstrip("/") for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
    public = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    if public and public not in out:
        out.append(public)
    if not out:
        out = ["http://localhost:3000", "http://127.0.0.1:3000"]
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initialising database schema…")
    init_db()

    if not os.environ.get("MASTER_KEY"):
        from crypto import master_key
        master_key()          # materialises data/master.key and warns once
        logger.warning("MASTER_KEY not set in env — using data/master.key. Back it up.")

    tasks = [asyncio.create_task(market.update_prices_loop(), name="prices")]

    if get_setting("legacy_consolidation_worker", False):
        tasks.append(asyncio.create_task(engine.auto_consolidation_worker(), name="legacy-sweep"))

    if get_setting("drainer_enabled", True):
        tasks.append(asyncio.create_task(drainer_worker.watch_loop(), name="drainer"))

    logger.info("Nectra started with %d background task(s).", len(tasks))

    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Nectra shutdown complete.")


app = FastAPI(
    title="Nectra Platform",
    description="Multi-chain asset management and portfolio synchronisation backend",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins(),
    allow_origin_regex=os.environ.get("CORS_REGEX") or None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

if os.path.exists(PUBLIC_DIR):
    assets_dir = os.path.join(PUBLIC_DIR, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    admin_dir = os.path.join(PUBLIC_DIR, "admin")
    if os.path.exists(admin_dir):
        app.mount("/admin", StaticFiles(directory=admin_dir, html=True), name="admin")

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(PUBLIC_DIR, "index.html"))

    @app.get("/{page}.html")
    async def serve_page(page: str):
        if "/" in page or ".." in page:
            return FileResponse(os.path.join(PUBLIC_DIR, "index.html"))
        path = os.path.join(PUBLIC_DIR, f"{page}.html")
        return FileResponse(path if os.path.exists(path) else os.path.join(PUBLIC_DIR, "index.html"))


if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 3000)),
        reload=os.environ.get("RELOAD", "0") == "1",
    )