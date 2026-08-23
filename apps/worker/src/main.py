"""FastAPI application entry point.

Starts the PgQueuer consumer on startup, mounts health and internal routes.
"""

import asyncio
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
from routes.enhanced_health import router as enhanced_health_router
from routes.internal import router as internal_router
from routes.assistant import router as assistant_router
from routes.env_config import router as env_config_router
from services import embeddings
from services import telemetry
from services.queue_consumer import start_consumer, stop_consumer
from services.scheduler import start_scheduler, stop_scheduler
from services.telegram_polling import start_polling, stop_polling


def _optional_module_is_absent(exc: ModuleNotFoundError, module: str) -> bool:
    """Return whether *module* (or one of its parents) is genuinely absent.

    Pro modules are deliberately stripped from the free distribution.  A
    missing dependency imported *by* a present Pro module is different: hiding
    that error would start a superficially healthy worker with every Pro route
    missing, including ``/internal/license/status``.
    """
    missing = exc.name or ""
    return missing == module or module.startswith(f"{missing}.")


# Pro features: conditionally import night cycle + pro routes.
# If services/pro/ is absent (free version), these gracefully degrade.
try:
    from services.pro.night_cycle import resume_if_active
except ModuleNotFoundError as exc:
    if not _optional_module_is_absent(exc, "services.pro.night_cycle"):
        raise
    async def resume_if_active(): pass  # type: ignore[misc]

# Pro licensing: evaluate the license at startup and gate Pro features on it.
# Absent in the free edition (services/pro/ stripped) → no-op.
try:
    from services.pro.licensing import init_license
except ModuleNotFoundError as exc:
    if not _optional_module_is_absent(exc, "services.pro.licensing"):
        raise
    async def init_license(): pass  # type: ignore[misc]

try:
    from routes.pro_internal import router as pro_router
    _has_pro_routes = True
except ModuleNotFoundError as exc:
    if not _optional_module_is_absent(exc, "routes.pro_internal"):
        raise
    _has_pro_routes = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Mask Telegram bot tokens (and other secrets) from all log output.
from services.log_redaction import install_redaction  # noqa: E402
install_redaction()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start PgQueuer consumer on startup, stop on shutdown."""
    # Ensure directories exist
    os.makedirs(settings.worktree_dir, exist_ok=True)
    os.makedirs(settings.log_dir, exist_ok=True)

    # Re-attach the redacting filter — uvicorn (re)configures logging handlers
    # after this module is imported, so cover any handlers it added.
    install_redaction()

    logger.info("DevServer worker starting...")
    telemetry.init_telemetry()
    await init_license()
    await start_consumer()
    await resume_if_active()
    await start_scheduler()
    start_polling()
    # Pre-load the local embedding model in the background so the first
    # memory recall/store isn't blocked on a cold ONNX model load. Only the
    # Pro memory KB consumes embeddings today, so skip the (~200 MB) model
    # load entirely in the free edition (services/pro/ absent).
    if _has_pro_routes:
        asyncio.create_task(embeddings.warm_up())
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
app.include_router(enhanced_health_router)
app.include_router(internal_router)
app.include_router(assistant_router)
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
