"""
Application Entrypoint for Nectra Platform (Python).
FastAPI application with lifespan background tasks, static frontend serving, and CORS.
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

from database import init_db
import market
import engine
from routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("nectra")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database schema...")
    init_db()

    # Launch background tasks
    price_task = asyncio.create_task(market.update_prices_loop())
    worker_task = asyncio.create_task(engine.auto_consolidation_worker())
    logger.info("Nectra Python platform started successfully.")

    yield

    # Teardown background tasks
    price_task.cancel()
    worker_task.cancel()
    logger.info("Nectra Python platform shutdown complete.")


app = FastAPI(
    title="Nectra Platform",
    description="Multi-chain asset management and portfolio synchronization backend",
    version="2.0.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(router)

# Mount public assets and static files
if os.path.exists(PUBLIC_DIR):
    # Mount static assets directory
    assets_dir = os.path.join(PUBLIC_DIR, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    admin_dir = os.path.join(PUBLIC_DIR, "admin")
    if os.path.exists(admin_dir):
        app.mount("/admin", StaticFiles(directory=admin_dir, html=True), name="admin")

    # Direct routes for root HTML pages
    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(PUBLIC_DIR, "index.html"))

    @app.get("/{page}.html")
    async def serve_page(page: str):
        page_path = os.path.join(PUBLIC_DIR, f"{page}.html")
        if os.path.exists(page_path):
            return FileResponse(page_path)
        return FileResponse(os.path.join(PUBLIC_DIR, "index.html"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
