"""Knowledge retrieval for the Ask Agent panel.

The assistant has to know DevServer itself — what a screen does, how to create
a task, why one is blocked, what Pro adds. That knowledge lives as curated,
user-facing prose in ``docs/assistant/``, selected here and injected into the
system prompt.

**Why not RAG over README.md / CLAUDE.md.** Those are ~165 KB of build detail
written for developers of DevServer, not operators of it; retrieving chunks of
them yields answers about migrations and module layout when the question was
"how do I create a task". The handbook is small enough that deterministic
keyword + route scoring beats embeddings here, costs nothing, and cannot drift.

Selection is: topics pinned to the current route always win, then the rest fill
a character budget by keyword score. Everything degrades — a missing docs
directory produces a smaller prompt, never an error.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "load_manifest",
    "select_topics",
    "build_system_prompt",
    "docs_root",
]

_MANIFEST_NAME = "00-manifest.json"
_DEFAULT_BUDGET = 9000
# A dynamic route segment in a pathname, e.g. /tasks/42 → /tasks/[id].
_ID_SEGMENT = re.compile(r"/\d+(?=/|$)")
_WORD = re.compile(r"[a-z0-9]+")


def docs_root() -> Path | None:
    """Locate ``docs/assistant`` across host and Docker layouts.

    The worker runs from ``apps/worker`` on a host checkout and from ``/app``
    inside the image (where ``docs/assistant`` is copied next to ``src``), so
    probe both rather than assuming one.
    """
    candidates: list[Path] = []
    env_root = os.environ.get("DEVSERVER_ROOT")
    if env_root:
        candidates.append(Path(env_root) / "docs" / "assistant")
    here = Path(__file__).resolve()
    # …/apps/worker/src/services/assistant_kb.py → repo root is parents[4]
    candidates.append(here.parents[4] / "docs" / "assistant")
    # Docker: /app/src/services/… → /app/docs/assistant
    candidates.append(here.parents[2] / "docs" / "assistant")
    candidates.append(Path.cwd() / "docs" / "assistant")
    for path in candidates:
        if (path / _MANIFEST_NAME).is_file():
            return path
    logger.warning(
        "Assistant knowledge pack not found; tried: %s",
        ", ".join(str(c) for c in candidates),
    )
    return None


@lru_cache(maxsize=1)
def load_manifest() -> dict[str, Any]:
    """Read and cache ``00-manifest.json``. Returns ``{}`` when unavailable."""
    root = docs_root()
    if root is None:
        return {}
    try:
        data = json.loads((root / _MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Assistant manifest unreadable: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=64)
def _read_topic(file_name: str) -> str:
    root = docs_root()
    if root is None:
        return ""
    # Manifest-controlled, but resolve-and-check anyway: a topic file must not
    # be able to name ../../.env.
    target = (root / file_name).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        logger.warning("Assistant topic escapes docs root: %r", file_name)
        return ""
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return ""


def normalize_route(path: str | None) -> str:
    """Turn a browser pathname into a manifest route key.

    ``/tasks/42`` → ``/tasks/[id]``; trailing slashes are dropped; ``''`` → ``/``.
    """
    if not path:
        return "/"
    route = _ID_SEGMENT.sub("/[id]", path.strip())
    route = route.rstrip("/") or "/"
    return route


def _score(topic: dict, question: str, route: str) -> tuple[float, float]:
    """Rank a topic as ``(keyword_score, context_score)``.

    The two are kept separate and sorted lexicographically rather than summed.
    A route pin guarantees a topic is *considered* — the operator's current
    screen is nearly always relevant — but it must not outrank a question that
    is plainly about something else ("how do I buy Pro?" asked on /logs).
    """
    words = set(_WORD.findall(question.lower()))
    lowered = question.lower()
    keyword = 0.0
    for kw in topic.get("keywords") or []:
        kw = kw.lower()
        if " " in kw:
            if kw in lowered:
                keyword += 6.0
        elif kw in words:
            keyword += 4.0

    context = 0.0
    if topic.get("always"):
        context += 50.0
    routes = topic.get("routes") or []
    if route in routes:
        context += 100.0
        # Several topics can pin the same route (e.g. /templates matters to both
        # "templates-ideas" and "task-types"). Break the tie toward the topic
        # that lists this route *first* — its primary screen.
        if routes[0] == route:
            context += 10.0
    return keyword, context


def select_topics(
    question: str,
    route: str | None = None,
    *,
    budget_chars: int = _DEFAULT_BUDGET,
) -> list[dict[str, str]]:
    """Pick the handbook topics to ground this turn.

    Returns ``[{"id", "title", "body"}]`` ordered most-relevant first, capped at
    ``budget_chars`` of body text.
    """
    manifest = load_manifest()
    topics = manifest.get("topics") or []
    if not topics:
        return []
    norm_route = normalize_route(route)
    scored = [(_score(t, question or "", norm_route), i, t) for i, t in enumerate(topics)]
    # Keyword relevance first, then screen context, then manifest order — so the
    # most on-topic handbook page leads and survives budget truncation.
    ranked = sorted(scored, key=lambda item: (item[0][0], item[0][1], -item[1]), reverse=True)
    chosen: list[dict[str, str]] = []
    used = 0
    for (keyword, context), _idx, topic in ranked:
        if keyword <= 0 and context <= 0:
            continue
        body = _read_topic(topic.get("file", "")).strip()
        if not body:
            continue
        if used + len(body) > budget_chars and chosen:
            continue  # try the next, smaller topic rather than stopping dead
        chosen.append(
            {"id": topic.get("id", ""), "title": topic.get("title", ""), "body": body}
        )
        used += len(body)
    return chosen


def render_page_context(page: dict | None, route: str | None = None) -> str:
    """Render the operator's current screen as a prompt block.

    ``route`` alone is enough: the manifest's ``screens`` map supplies a
    description, so the assistant still knows where the operator is even when
    the client sent no rich page object. Without this the model answers "I can't
    see which page you're on" while the route was known all along.
    """
    page = dict(page or {})
    path = page.get("path") or page.get("route") or route
    if not path:
        return ""
    norm = normalize_route(path)
    lines = ["## Where the operator is right now", f"- Page: `{path}`"]
    title = page.get("title") or (load_manifest().get("screens") or {}).get(norm)
    if title:
        lines.append(f"- Screen: {title}")
    entity = page.get("entity") or {}
    if entity:
        kind = entity.get("kind") or "item"
        bits = [
            f"{k}={v}"
            for k, v in entity.items()
            if k != "kind" and v not in (None, "", [])
        ]
        lines.append(f"- Viewing {kind}: " + ", ".join(bits) if bits else f"- Viewing {kind}")
    lines.append(
        "A question asked here is usually about this screen — and, when one is "
        "named above, about that specific item. Answer in that context unless "
        "the operator clearly means something else."
    )
    return "\n".join(lines)


_BASE_RULES = """\
You are the Ask Agent assistant built into the DevServer dashboard.

Answer using the DevServer handbook below. It is authoritative: prefer it over
anything you think you remember about DevServer. If the handbook does not cover
something, say so rather than inventing behaviour, screens or settings — the
operator will act on what you say.

Be concise and concrete. Name the actual screen, button or field. Give one
correct next step rather than a list of possibilities. Use markdown sparingly:
short paragraphs, a list only when the content is genuinely a list.\
"""

_FREE_RULES = """\
## Your capabilities right now

You have NO tools. You cannot read the operator's repositories, query their
database, inspect their tasks, or run anything. You can only answer from the
handbook and from what the operator tells you.

When a question needs data you do not have ("why did MY task fail?", "what's in
MY repo?"), say plainly that you cannot see it in this edition, and tell them
exactly where to look — which page, which panel, which log.

Tool access, semantic search over the operator's repositories, and one-click
fixes are DevServer Pro features. Mention that only when it genuinely answers
their question; do not advertise it unprompted in every reply.\
"""

_PRO_RULES = """\
## Your capabilities right now

You have read-only tools: semantic code and doc search across the operator's
repositories, the per-repo memory knowledge base, prior decisions and
transcripts, the repo map, and live task state. Use them before answering
anything about the operator's own code, tasks or history — do not guess when
you can look.

You cannot change anything directly. When something needs doing — running a
maintenance script, creating a task — call `propose_action` and STOP. The
operator approves with one click and you will be told the outcome. Propose one
concrete action, say in a sentence why it is the right one, and never propose
anything they did not ask for.\
"""


def build_system_prompt(
    topics: list[dict[str, str]],
    page: dict | None = None,
    *,
    route: str | None = None,
    edition: str = "free",
    tools_enabled: bool = False,
) -> str:
    """Assemble the system prompt: rules + capabilities + page + handbook."""
    parts = [_BASE_RULES, _PRO_RULES if tools_enabled else _FREE_RULES]
    page_block = render_page_context(page, route)
    if page_block:
        parts.append(page_block)
    if topics:
        handbook = ["## DevServer handbook"]
        for topic in topics:
            handbook.append(topic["body"].strip())
        parts.append("\n\n---\n\n".join(handbook))
    else:
        parts.append(
            "## DevServer handbook\n\n"
            "(The handbook could not be loaded on this deployment. Answer only "
            "what you are confident about and say when you are unsure.)"
        )
    parts.append(f"(Edition: {edition}.)")
    return "\n\n".join(parts)


def reset_cache() -> None:
    """Drop cached manifest/topic reads — for tests and hot edits."""
    load_manifest.cache_clear()
    _read_topic.cache_clear()
