"""Confluence import source — search/fetch pages, convert to Markdown.

Talks to the Confluence REST API (Cloud or Data Center):

    GET {base}/rest/api/space                    — list spaces (scopes)
    GET {base}/rest/api/content/search?cql=...   — CQL search
    GET {base}/rest/api/content/{id}             — fetch page + storage body

Auth — resolved per call, per-repo overrides win over global config:
    - Cloud:  account email + API token  → HTTP Basic
    - DC:     Personal Access Token only → ``Authorization: Bearer``

The page body arrives in Confluence *storage format* (XHTML with ``ac:``/
``ri:`` extension tags). :func:`storage_to_markdown` converts it with the
stdlib ``html.parser`` — deliberately no new dependency; fidelity is
pragmatic (headings, lists, code macros, tables, links), not pixel-perfect.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any

import httpx

from config import settings
from services.import_sources.base import ImportDraft, ImportSource, register

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


class ConfluenceNotConfiguredError(RuntimeError):
    """Raised when neither the repo nor the global env carries credentials."""


# ─── Storage-format XHTML → Markdown ─────────────────────────────────────────

_HEADING = {f"h{i}": "#" * i for i in range(1, 7)}


class _StorageToMarkdown(HTMLParser):
    """Single-pass converter for Confluence storage-format XHTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._list_stack: list[str] = []      # 'ul' | 'ol' nesting
        self._href: str | None = None
        self._in_pre = False
        self._in_code = False
        # ac:structured-macro name="code" handling
        self._macro_stack: list[str] = []
        self._code_macro_lang = ""
        self._code_macro_body: list[str] = []
        self._param_name: str | None = None
        # tables
        self._in_table = False
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._header_done = False

    # ── emit helpers ────────────────────────────────────────────────────
    def _text(self, s: str) -> None:
        """Route text to the innermost active buffer."""
        if self._macro_stack:
            self._code_macro_body.append(s)
        elif self._cell is not None:
            self._cell.append(s.replace("|", "\\|").replace("\n", " "))
        else:
            self.out.append(s)

    def _break(self, n: int = 2) -> None:
        """Ensure a paragraph (or line) break in the main output."""
        if self._cell is not None or self._macro_stack:
            return
        joined = "".join(self.out)
        if not joined or joined.endswith("\n" * n):
            return
        while not joined.endswith("\n" * n):
            joined += "\n"
        self.out = [joined]

    # ── tag handling ────────────────────────────────────────────────────
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag.startswith("ac:") or tag.startswith("ri:"):
            if tag == "ac:structured-macro":
                self._macro_stack.append(a.get("ac:name") or "")
                if (a.get("ac:name") or "") == "code":
                    self._code_macro_lang = ""
                    self._code_macro_body = []
            elif tag == "ac:parameter" and self._macro_stack:
                self._param_name = a.get("ac:name") or ""
            return
        if tag in _HEADING:
            self._break()
            self._text(_HEADING[tag] + " ")
        elif tag == "p":
            self._break()
        elif tag == "br":
            self._text("\n")
        elif tag == "hr":
            self._break()
            self._text("---")
            self._break()
        elif tag in ("strong", "b"):
            self._text("**")
        elif tag in ("em", "i"):
            self._text("*")
        elif tag == "code" and not self._in_pre:
            self._in_code = True
            self._text("`")
        elif tag == "pre":
            self._in_pre = True
            self._break()
            self._text("```\n")
        elif tag in ("ul", "ol"):
            if not self._list_stack:
                self._break()
            self._list_stack.append(tag)
        elif tag == "li":
            indent = "  " * (len(self._list_stack) - 1)
            marker = "1." if self._list_stack and self._list_stack[-1] == "ol" else "-"
            self._break(1)
            self._text(f"{indent}{marker} ")
        elif tag == "a":
            self._href = a.get("href")
            self._text("[")
        elif tag == "table":
            self._in_table = True
            self._header_done = False
            self._break()
        elif tag == "tr" and self._in_table:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "blockquote":
            self._break()
            self._text("> ")

    def handle_endtag(self, tag: str) -> None:
        if tag.startswith("ac:") or tag.startswith("ri:"):
            if tag == "ac:parameter":
                self._param_name = None
            elif tag == "ac:structured-macro" and self._macro_stack:
                name = self._macro_stack.pop()
                if name == "code" and not self._macro_stack:
                    body = "".join(self._code_macro_body).strip("\n")
                    self._break()
                    self.out.append(f"```{self._code_macro_lang}\n{body}\n```")
                    self._break()
            return
        if tag in _HEADING or tag == "p":
            self._break()
        elif tag in ("strong", "b"):
            self._text("**")
        elif tag in ("em", "i"):
            self._text("*")
        elif tag == "code" and self._in_code:
            self._in_code = False
            self._text("`")
        elif tag == "pre":
            self._in_pre = False
            self._text("\n```")
            self._break()
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            if not self._list_stack:
                self._break()
        elif tag == "a":
            href = self._href or ""
            self._href = None
            self._text(f"]({href})" if href else "]")
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            cells = self._row
            self._row = None
            if cells:
                self.out.append("| " + " | ".join(cells) + " |\n")
                if not self._header_done:
                    self.out.append("|" + "---|" * len(cells) + "\n")
                    self._header_done = True
        elif tag == "table":
            self._in_table = False
            self._break()

    def handle_data(self, data: str) -> None:
        if self._param_name is not None:
            # ac:parameter payload — capture the code macro's language
            if self._param_name == "language" and self._macro_stack and self._macro_stack[-1] == "code":
                self._code_macro_lang = data.strip()
            return
        if self._in_pre or self._macro_stack:
            self._text(data)
        else:
            self._text(re.sub(r"\s+", " ", data))

    def unknown_decl(self, data: str) -> None:
        # CDATA sections (code macro bodies): data == "CDATA[...literal..."
        if data.startswith("CDATA["):
            self._text(data[len("CDATA["):])

    def result(self) -> str:
        text = "".join(self.out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"


def storage_to_markdown(xhtml: str) -> str:
    """Convert Confluence storage-format XHTML to Markdown (best effort)."""
    if not xhtml:
        return ""
    parser = _StorageToMarkdown()
    try:
        parser.feed(xhtml)
        parser.close()
        return parser.result()
    except Exception:  # noqa: BLE001 — never let a weird page kill an import
        logger.warning("storage→markdown conversion failed; falling back to tag-strip", exc_info=True)
        return re.sub(r"<[^>]+>", " ", xhtml).strip()


# ─── The source adapter ──────────────────────────────────────────────────────

class ConfluenceSource(ImportSource):
    name = "confluence"
    label = "Confluence"

    # ── credentials ─────────────────────────────────────────────────────
    def _creds(self, repo: Any | None) -> tuple[str, str, str]:
        """Return (base_url, username, token) — repo override wins when it
        carries a URL; otherwise the global env config."""
        if repo is not None and (getattr(repo, "confluence_url", "") or "").strip():
            return (
                repo.confluence_url.strip(),
                (getattr(repo, "confluence_username", "") or "").strip(),
                (getattr(repo, "confluence_token", "") or "").strip(),
            )
        return (
            (settings.confluence_url or "").strip(),
            (settings.confluence_username or "").strip(),
            (settings.confluence_api_token or "").strip(),
        )

    def is_configured(self, repo: Any | None = None) -> bool:
        url, _user, token = self._creds(repo)
        return bool(url and token)

    @staticmethod
    def _api_base(url: str) -> str:
        """Normalise the base URL. Atlassian Cloud serves Confluence under
        ``/wiki``; accept both ``https://x.atlassian.net`` and
        ``https://x.atlassian.net/wiki`` from the operator."""
        base = url.rstrip("/")
        host = re.sub(r"^https?://", "", base).split("/")[0]
        if host.endswith(".atlassian.net") and not base.endswith("/wiki"):
            base += "/wiki"
        return base

    def _client(self, repo: Any | None) -> tuple[httpx.AsyncClient, str]:
        url, user, token = self._creds(repo)
        if not (url and token):
            raise ConfluenceNotConfiguredError(
                "Confluence is not configured. Set CONFLUENCE_URL + "
                "CONFLUENCE_API_TOKEN (+ CONFLUENCE_USERNAME for Cloud) in "
                "Settings, or per-repo overrides."
            )
        base = self._api_base(url)
        if user:
            client = httpx.AsyncClient(auth=(user, token), timeout=_TIMEOUT)
        else:
            client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, timeout=_TIMEOUT
            )
        return client, base

    # ── API calls ───────────────────────────────────────────────────────
    async def list_scopes(self, repo: Any | None = None) -> list[dict]:
        client, base = self._client(repo)
        async with client:
            res = await client.get(
                f"{base}/rest/api/space",
                params={"limit": 100, "type": "global"},
            )
            res.raise_for_status()
            data = res.json()
        return [
            {"key": s.get("key", ""), "name": s.get("name", "")}
            for s in data.get("results", [])
        ]

    async def search(
        self,
        query: str,
        scope: str | None = None,
        limit: int = 25,
        repo: Any | None = None,
    ) -> list[dict]:
        q = (query or "").strip()
        if q.lower().startswith("cql:"):
            cql = q[4:].strip()
        else:
            clauses = ["type=page"]
            if q:
                clauses.append('text ~ "%s"' % q.replace('"', '\\"'))
            if scope:
                clauses.append('space="%s"' % scope.replace('"', ""))
            cql = " AND ".join(clauses)

        client, base = self._client(repo)
        async with client:
            res = await client.get(
                f"{base}/rest/api/content/search",
                params={"cql": cql, "limit": max(1, min(limit, 100)), "expand": "version,space"},
            )
            res.raise_for_status()
            data = res.json()

        items: list[dict] = []
        for row in data.get("results", []):
            links = row.get("_links", {}) or {}
            web_base = links.get("base") or base
            items.append({
                "id": str(row.get("id", "")),
                "title": row.get("title", ""),
                "scope": (row.get("space") or {}).get("key", ""),
                "version": str((row.get("version") or {}).get("number", "")),
                "updated": ((row.get("version") or {}).get("when") or ""),
                "url": web_base + (links.get("webui") or ""),
            })
        return items

    async def get(self, external_id: str, repo: Any | None = None) -> dict:
        client, base = self._client(repo)
        async with client:
            res = await client.get(
                f"{base}/rest/api/content/{external_id}",
                params={"expand": "body.storage,version,space"},
            )
            res.raise_for_status()
            item = res.json()
        item["_api_base"] = base
        return item

    def to_draft(self, item: dict) -> ImportDraft:
        links = item.get("_links", {}) or {}
        web_base = links.get("base") or item.get("_api_base", "")
        xhtml = ((item.get("body") or {}).get("storage") or {}).get("value", "")
        version = (item.get("version") or {}).get("number", "")
        return ImportDraft(
            source=self.name,
            external_id=str(item.get("id", "")),
            external_rev=str(version),
            title=item.get("title", ""),
            markdown=storage_to_markdown(xhtml),
            scope=(item.get("space") or {}).get("key", ""),
            source_url=web_base + (links.get("webui") or ""),
            metadata={
                "updated": ((item.get("version") or {}).get("when") or ""),
                "type": item.get("type", "page"),
            },
        )

    async def ping(self, repo: Any | None = None) -> dict:
        try:
            client, base = self._client(repo)
            async with client:
                res = await client.get(f"{base}/rest/api/space", params={"limit": 1})
                res.raise_for_status()
            return {"ok": True}
        except ConfluenceNotConfiguredError as exc:
            return {"ok": False, "error": str(exc)}
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "error": f"Confluence returned HTTP {exc.response.status_code} "
                         f"({'auth' if exc.response.status_code in (401, 403) else 'request'} problem)",
            }
        except Exception as exc:  # noqa: BLE001 — verdict, not crash
            return {"ok": False, "error": str(exc)}


register(ConfluenceSource())
