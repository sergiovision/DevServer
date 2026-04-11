"""Standalone PgQueuer worker entry point — no FastAPI, just the queue consumer.

Usage: python -m worker
"""

import asyncio
import logging
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(__file__))

from config import settings
from services.queue_consumer import start_consumer, stop_consumer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_shutdown = asyncio.Event()


def _signal_handler():
    logger.info("Shutdown signal received")
    _shutdown.set()


async def main():
    os.makedirs(settings.worktree_dir, exist_ok=True)
    os.makedirs(settings.log_dir, exist_ok=True)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    logger.info("DevServer standalone worker starting (concurrency=%d)...", settings.worker_concurrency)
    await start_consumer()
    logger.info("Worker ready. Waiting for jobs...")

    await _shutdown.wait()

    logger.info("Shutting down...")
    await stop_consumer()
    logger.info("Worker stopped.")


if __name__ == "__main__":
    asyncio.run(main())
