"""FastAPI application entry point.

Starts the PgQueuer consumer on startup, mounts health and internal routes.
"""

import asyncio
import contextlib
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
from routes import generation, registry, repos, system, task_actions, task_artifacts, tasks
from routes.assistant import router as assistant_router
from routes.enhanced_health import router as enhanced_health_router
from routes.env_config import router as env_config_router
from routes.health import router as health_router
from services import embeddings, telemetry
from services.queue_consumer import start_consumer, stop_consumer
from services.scheduler import start_scheduler, stop_scheduler
from services.telegram_polling import start_polling, stop_polling

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Mask Telegram bot tokens (and other secrets) from all log output.
from services.log_redaction import install_redaction

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
    await start_consumer()
    await start_scheduler()
    start_polling()
    # Load the embedding model in the background so the first memory recall
    # is not blocked on a cold ONNX load. The task is kept so shutdown can
    # cancel it instead of leaving it dangling.
    warm_up = asyncio.create_task(embeddings.warm_up(), name="embeddings-warm-up")
    logger.info(
        "DevServer worker ready (port=%d, concurrency=%d)",
        settings.worker_port,
        settings.worker_concurrency,
    )
    yield
    logger.info("DevServer worker shutting down...")
    warm_up.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await warm_up
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
for module in (tasks, task_actions, task_artifacts, repos, system, generation, registry):
    app.include_router(module.router)
app.include_router(assistant_router)
app.include_router(env_config_router)


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
