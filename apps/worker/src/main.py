"""FastAPI application entry point.

Starts the PgQueuer consumer on startup, mounts health and internal routes.
"""

import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

# Ensure src/ is on the path when running directly (e.g. python src/main.py)
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from config import settings
from routes.health import router as health_router
from routes.internal import router as internal_router
from routes.env_config import router as env_config_router
from services.queue_consumer import start_consumer, stop_consumer
from services.scheduler import start_scheduler, stop_scheduler
from services.telegram_polling import start_polling, stop_polling

# Pro features: conditionally import night cycle + pro routes.
# If services/pro/ is absent (free version), these gracefully degrade.
try:
    from services.pro.night_cycle import resume_if_active
except ImportError:
    async def resume_if_active(): pass  # type: ignore[misc]

try:
    from routes.pro_internal import router as pro_router
    _has_pro_routes = True
except ImportError:
    _has_pro_routes = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start PgQueuer consumer on startup, stop on shutdown."""
    # Ensure directories exist
    os.makedirs(settings.worktree_dir, exist_ok=True)
    os.makedirs(settings.log_dir, exist_ok=True)

    logger.info("DevServer worker starting...")
    await start_consumer()
    await resume_if_active()
    await start_scheduler()
    start_polling()
    logger.info("DevServer worker ready (port=%d, concurrency=%d)",
                settings.worker_port, settings.worker_concurrency)
    yield
    logger.info("DevServer worker shutting down...")
    await stop_polling()
    await stop_scheduler()
    await stop_consumer()


app = FastAPI(
    title="DevServer Worker",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(internal_router)
app.include_router(env_config_router)
if _has_pro_routes:
    app.include_router(pro_router)


def run():
    """Entry point for pyproject.toml scripts."""
    uvicorn.run(
        "main:app",
        host=settings.worker_host,
        port=settings.worker_port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
