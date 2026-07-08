"""ImportSource ABC + ImportDraft — the pluggable external-import contract.

A source adapter wraps one external system (Confluence today; Jira/Azure
DevOps are deliberately out of scope for now). The contract is read-only:
adapters never write to the external system and never touch the DevServer
database — they fetch and normalise. Target creation (tasks/ideas), queueing
and the ``external_imports`` idempotency ledger live in the Next.js API layer.

Credential resolution is per-call: every method accepts an optional ``repo``
(the SQLAlchemy ``Repo`` row) whose ``confluence_*``-style override columns,
when set, win over the global ``config.settings`` values.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImportDraft:
    """A source document normalised for import — what the UI previews and
    what the web layer persists as a Task description / Idea content."""

    source: str                    # source name, e.g. 'confluence'
    external_id: str               # source-native document id
    external_rev: str              # revision marker (Confluence version number)
    title: str
    markdown: str                  # body converted to Markdown
    scope: str = ""                # container the doc lives in (space key)
    source_url: str = ""           # human-clickable link back to the source
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "external_id": self.external_id,
            "external_rev": self.external_rev,
            "title": self.title,
            "markdown": self.markdown,
            "scope": self.scope,
            "source_url": self.source_url,
            "metadata": self.metadata,
        }


class ImportSource(ABC):
    """Abstract interface every external import source implements."""

    #: Short identifier used in URLs and the ``external_imports.source`` column.
    name: str = ""
    #: Human-readable label for the UI source selector.
    label: str = ""

    @abstractmethod
    def is_configured(self, repo: Any | None = None) -> bool:
        """True when enough credentials exist (repo override or global) to talk
        to the source."""

    @abstractmethod
    async def list_scopes(self, repo: Any | None = None) -> list[dict]:
        """Return the source's containers — ``[{key, name}]`` (spaces, …)."""

    @abstractmethod
    async def search(
        self,
        query: str,
        scope: str | None = None,
        limit: int = 25,
        repo: Any | None = None,
    ) -> list[dict]:
        """Search documents; returns lightweight rows for the results table
        (id, title, scope, version, url, updated) — no bodies."""

    @abstractmethod
    async def get(self, external_id: str, repo: Any | None = None) -> dict:
        """Fetch one document including its body, raw from the source API."""

    @abstractmethod
    def to_draft(self, item: dict) -> ImportDraft:
        """Normalise a raw :meth:`get` payload into an :class:`ImportDraft`."""

    async def ping(self, repo: Any | None = None) -> dict:
        """Cheap connectivity + auth probe. Returns ``{ok: bool, error?: str}``
        — never raises, so the UI always gets a clean verdict."""
        try:
            await self.list_scopes(repo=repo)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001 — verdict, not crash
            return {"ok": False, "error": str(exc)}


# ─── Registry ────────────────────────────────────────────────────────────────
# Populated at the bottom of this package's __init__ import chain; adapters
# register themselves here so routes can do a name → adapter lookup.

SOURCES: dict[str, ImportSource] = {}


def register(source: ImportSource) -> ImportSource:
    SOURCES[source.name] = source
    return source


def get_source(name: str) -> ImportSource | None:
    return SOURCES.get(name)
