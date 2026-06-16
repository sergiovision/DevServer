"""Local text embeddings for semantic memory recall.

Fully local — no cloud API, no API key. Backed by fastembed (ONNX runtime,
CPU). This replaces the old Voyage AI HTTP path that was keyed (incorrectly)
on ``ANTHROPIC_API_KEY`` and silently degraded recall to plain text search
whenever the key was absent or not a Voyage key.

The model is loaded once, lazily, and cached process-wide. fastembed is
synchronous, so embedding runs in a worker thread (``asyncio.to_thread``)
to avoid blocking the event loop.

The embedding dimension is fixed at the ``agent_memory.embedding`` column
DDL (``vector(768)``). Swapping ``EMBEDDING_MODEL`` to a model with a
different dimension therefore requires a schema migration + re-embed — see
``scripts/reembed_memory.py``. Both bundled defaults are 768-dim so they
interchange without a migration.

This module is FREE-level (no ``pro`` dependency) and survives
``strip-pro.sh``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import threading

from config import settings

logger = logging.getLogger(__name__)

# Known fastembed models -> output dimension. The DB column is ``vector(768)``,
# so the default and any drop-in replacement must be 768-dim. Models with a
# different dimension (e.g. the 384-dim small models) require a column
# migration before use.
_MODEL_DIM = {
    "BAAI/bge-base-en-v1.5": 768,
    "jinaai/jina-embeddings-v2-base-code": 768,
    "jinaai/jina-embeddings-v2-base-en": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}

_DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"
MODEL_ID = settings.embedding_model or _DEFAULT_MODEL
DIM = _MODEL_DIM.get(MODEL_ID, 768)

# Cap input length so a runaway transcript can't blow up the encoder.
_MAX_CHARS = 8000

_model = None
_model_lock = threading.Lock()


def _fastembed_cache_dir() -> str:
    """Resolve fastembed's on-disk cache dir (mirrors ``define_cache_dir``)."""
    default = os.path.join(tempfile.gettempdir(), "fastembed_cache")
    return os.getenv("FASTEMBED_CACHE_PATH", default)


def _purge_model_cache(model_id: str) -> None:
    """Delete a model's (possibly half-downloaded) cache folder. Best-effort.

    A killed/interrupted download leaves the HF snapshot dir in place with the
    ``onnx/model.onnx`` file missing; fastembed then surfaces that as a
    ``NoSuchFile`` load error on every subsequent attempt instead of
    re-fetching it. Removing the folder forces a clean re-download.
    """
    folder = "models--" + model_id.replace("/", "--")
    path = os.path.join(_fastembed_cache_dir(), folder)
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            logger.info("purged incomplete embedding cache: %s", path)
    except Exception:
        logger.warning("could not purge embedding cache at %s", path, exc_info=True)


def _load_text_embedding(model_id: str):
    """Construct a fastembed model, self-healing a corrupt cache.

    On a load failure (typically a missing ONNX file from an interrupted
    download), purge the model's cache folder and retry once with a clean
    download. The second failure propagates to the caller.
    """
    from fastembed import TextEmbedding  # heavy import, defer

    try:
        return TextEmbedding(model_name=model_id)
    except Exception:
        logger.warning(
            "embedding model %s failed to load; purging cache and retrying",
            model_id,
            exc_info=True,
        )
        _purge_model_cache(model_id)
        return TextEmbedding(model_name=model_id)


def _get_model():
    """Lazily load + cache the fastembed model (thread-safe, double-checked)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                logger.info("Loading local embedding model: %s (%dd)", MODEL_ID, DIM)
                try:
                    _model = _load_text_embedding(MODEL_ID)
                except Exception:
                    if MODEL_ID == _DEFAULT_MODEL:
                        raise
                    logger.warning(
                        "embedding model %s unavailable; falling back to %s",
                        MODEL_ID,
                        _DEFAULT_MODEL,
                        exc_info=True,
                    )
                    _model = _load_text_embedding(_DEFAULT_MODEL)
    return _model


def _embed_sync(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    # fastembed yields one numpy array per input, in order.
    return [vec.tolist() for vec in model.embed(texts)]


async def embed(text: str) -> list[float] | None:
    """Embed a single string. Returns ``None`` on empty input or any failure.

    Never raises — callers treat ``None`` as "store/search without a vector",
    falling back to lexical/text search.
    """
    if not text or not text.strip():
        return None
    try:
        vecs = await asyncio.to_thread(_embed_sync, [text[:_MAX_CHARS]])
        return vecs[0] if vecs else None
    except Exception:
        logger.exception("local embedding failed (model=%s)", MODEL_ID)
        return None


async def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """Embed many strings at once. Returns a list aligned with ``texts``.

    On a batch failure every entry is ``None``. Never raises.
    """
    if not texts:
        return []
    clean = [(t or "")[:_MAX_CHARS] for t in texts]
    try:
        vecs = await asyncio.to_thread(_embed_sync, clean)
        return list(vecs)
    except Exception:
        logger.exception("local batch embedding failed (model=%s)", MODEL_ID)
        return [None] * len(texts)


async def warm_up() -> None:
    """Pre-load the model so the first real request isn't slow. Best-effort.

    Call once on worker startup. Failure is logged, not fatal — the model
    will be retried lazily on first ``embed()``.
    """
    try:
        await asyncio.to_thread(_get_model)
        logger.info("Embedding model warmed up: %s", MODEL_ID)
    except Exception:
        logger.warning("embedding warm-up failed; will retry lazily", exc_info=True)
