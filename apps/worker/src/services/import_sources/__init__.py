"""Pluggable external import sources (Confluence first).

Each source knows how to authenticate against one external system, list its
scopes (spaces/projects), search documents, fetch a single document, and
normalise it into an :class:`~services.import_sources.base.ImportDraft` —
title + Markdown body + revision marker. The worker exposes these through
``/internal/import/*``; the Next.js side owns target insertion (tasks/ideas),
enqueueing, and the ``external_imports`` idempotency history.
"""

from services.import_sources.base import ImportDraft, ImportSource, SOURCES, get_source
from services.import_sources.confluence import ConfluenceSource

__all__ = ["ImportDraft", "ImportSource", "SOURCES", "get_source", "ConfluenceSource"]
