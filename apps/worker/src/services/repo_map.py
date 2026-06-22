"""Repo map — lightweight symbol index of a worktree for LLM prompt enrichment.

The goal is to give Claude a high-signal, token-budgeted view of the repo
structure BEFORE it starts reading files, so it stops hallucinating file paths
and symbols that don't exist.

Design choices:
- No native dependencies (no tree-sitter). Regex-based per-language extractors.
- Skips vendor / build / binary / huge files and .gitignored directories.
- Budget-aware: returns the top-N files by importance (roots first, then by
  symbol density), truncated to fit in ``max_chars``.
- Pure-async API so it plays nicely with the rest of the worker.
- Never raises — a broken file or unknown language is silently skipped.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Directories to skip entirely. Matched against the directory *name*, not path.
SKIP_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", ".next", ".nuxt", ".turbo", ".cache",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env", ".env",
    "dist", "build", "out", "target", "bin", "obj",
    ".idea", ".vscode",
    "coverage", ".nyc_output",
    "vendor", "third_party",
    ".devserver",  # our own cache directory
    "worktrees",   # DevServer's per-task sub-worktrees (avoid re-indexing)
}

# File extensions → language key. Keep this list tight; anything not in here
# gets a filename-only entry with no symbols.
EXT_LANG = {
    ".py": "python",
    ".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".sql": "sql",
    ".sh": "bash", ".bash": "bash",
}

# Files that should always show up in the map regardless of extension.
IMPORTANT_FILENAMES = {
    "CLAUDE.md", "README.md", "README", "AGENTS.md",
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "Makefile",
}

# Max file size (bytes) we will read for symbol extraction. Files larger than
# this get a filename-only entry — they are usually generated or vendored.
MAX_FILE_BYTES = 200_000

# Max output length of the rendered repo map (characters, roughly chars/4 = tokens).
# Originally 8 KB, halved to 4 KB after Phase 1 testing showed the larger map
# pushed concurrent tasks past the default 30K-tokens/minute Anthropic rate
# limit. 4 KB ≈ 1K tokens still gives the agent enough symbol surface area
# to stop hallucinating file paths, which is the only thing the map is for.
DEFAULT_MAX_CHARS = 4_000


# ── Regex patterns per language ───────────────────────────────────────────────
# Each pattern matches top-level declarations. We intentionally miss nested
# definitions to keep signal-to-noise high. Anchors at start-of-line.

PATTERNS: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "python": [
        ("class", re.compile(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("def",   re.compile(r"^(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
    ],
    "ts": [
        ("class",     re.compile(r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)),
        ("interface", re.compile(r"^(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)", re.M)),
        ("type",      re.compile(r"^(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*=", re.M)),
        ("enum",      re.compile(r"^(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)", re.M)),
        ("function",  re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)", re.M)),
        ("const",     re.compile(r"^(?:export\s+)?(?:const|let)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?\(", re.M)),
    ],
    "js": [
        ("class",    re.compile(r"^(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", re.M)),
        ("function", re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)", re.M)),
        ("const",    re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", re.M)),
    ],
    "csharp": [
        ("class",     re.compile(r"^\s*(?:public|internal|private|protected)?\s*(?:static\s+|sealed\s+|abstract\s+|partial\s+)*class\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("interface", re.compile(r"^\s*(?:public|internal|private|protected)?\s*interface\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("record",    re.compile(r"^\s*(?:public|internal|private|protected)?\s*(?:sealed\s+)?record\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("struct",    re.compile(r"^\s*(?:public|internal|private|protected)?\s*(?:readonly\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("enum",      re.compile(r"^\s*(?:public|internal|private|protected)?\s*enum\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
    ],
    "go": [
        ("func", re.compile(r"^func\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("type", re.compile(r"^type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)", re.M)),
    ],
    "rust": [
        ("fn",     re.compile(r"^(?:pub\s+(?:\([^)]*\)\s*)?)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("struct", re.compile(r"^(?:pub\s+(?:\([^)]*\)\s*)?)?struct\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("enum",   re.compile(r"^(?:pub\s+(?:\([^)]*\)\s*)?)?enum\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("trait",  re.compile(r"^(?:pub\s+(?:\([^)]*\)\s*)?)?trait\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("impl",   re.compile(r"^impl(?:<[^>]+>)?\s+(?:[A-Za-z_][\w:]*\s+for\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.M)),
    ],
    "java": [
        ("class",     re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+|final\s+|abstract\s+)*class\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("interface", re.compile(r"^\s*(?:public|private|protected)?\s*interface\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("enum",      re.compile(r"^\s*(?:public|private|protected)?\s*enum\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
    ],
    "kotlin": [
        ("class",   re.compile(r"^\s*(?:open\s+|sealed\s+|abstract\s+|data\s+)*class\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("object",  re.compile(r"^\s*object\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("fun",     re.compile(r"^\s*(?:suspend\s+)?fun\s+(?:<[^>]+>\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.M)),
    ],
    "ruby": [
        ("class",  re.compile(r"^\s*class\s+([A-Z][A-Za-z0-9_]*)", re.M)),
        ("module", re.compile(r"^\s*module\s+([A-Z][A-Za-z0-9_]*)", re.M)),
        ("def",    re.compile(r"^\s*def\s+(?:self\.)?([a-z_][A-Za-z0-9_?!=]*)", re.M)),
    ],
    "php": [
        ("class",     re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("function",  re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
        ("interface", re.compile(r"^\s*interface\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)),
    ],
    "sql": [
        ("table",    re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([A-Za-z_][A-Za-z0-9_.]*)\"?", re.M | re.I)),
        ("function", re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+\"?([A-Za-z_][A-Za-z0-9_.]*)\"?", re.M | re.I)),
        ("view",     re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+\"?([A-Za-z_][A-Za-z0-9_.]*)\"?", re.M | re.I)),
    ],
    "bash": [
        ("func", re.compile(r"^(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", re.M)),
    ],
}


@dataclass
class FileEntry:
    rel_path: str
    lang: str
    symbols: list[tuple[str, str]] = field(default_factory=list)  # (kind, name)
    size_bytes: int = 0

    @property
    def score(self) -> int:
        """Rough importance score: root files and symbol-dense files rank higher."""
        depth = self.rel_path.count("/")
        symbol_score = min(len(self.symbols), 20)
        # Shallower files are more likely to be entry points.
        return symbol_score * 10 - depth * 3


def _should_skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".") and name not in {".github", ".claude"}


def _extract_symbols(text: str, lang: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, pattern in PATTERNS.get(lang, []):
        for match in pattern.finditer(text):
            name = match.group(1)
            key = (kind, name)
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
            if len(out) >= 30:  # per-file cap — avoids generated-code floods
                return out
    return out


def _scan_worktree(worktree_path: str, max_files: int, subdir: str | None = None) -> list[FileEntry]:
    entries: list[FileEntry] = []
    # Scan a sub-tree when requested (RLM E4 drill-down) but keep rel_path
    # relative to the repo root so rendered paths stay meaningful.
    scan_root = os.path.join(worktree_path, subdir) if subdir else worktree_path
    if not os.path.isdir(scan_root):
        return entries

    for dirpath, dirnames, filenames in os.walk(scan_root):
        # Prune skip dirs in place so os.walk doesn't descend into them.
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

        for filename in filenames:
            if len(entries) >= max_files:
                return entries

            full_path = os.path.join(dirpath, filename)
            # Normalise to forward slashes so rendered paths are consistent
            # across OSes (git, the corpus, and allow-list checks all use "/").
            rel_path = os.path.relpath(full_path, worktree_path).replace(os.sep, "/")

            ext = os.path.splitext(filename)[1].lower()
            lang = EXT_LANG.get(ext, "")

            is_important = filename in IMPORTANT_FILENAMES
            if not lang and not is_important:
                continue

            try:
                size = os.path.getsize(full_path)
            except OSError:
                continue

            entry = FileEntry(rel_path=rel_path, lang=lang or "misc", size_bytes=size)

            if lang and size <= MAX_FILE_BYTES:
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    entry.symbols = _extract_symbols(text, lang)
                except OSError:
                    pass

            entries.append(entry)

    return entries


def _render(entries: list[FileEntry], max_chars: int) -> str:
    """Render entries as a compact, deterministic text block."""
    if not entries:
        return "(repo map: no indexable files found)"

    # Sort by score desc, then by path for stability.
    entries_sorted = sorted(entries, key=lambda e: (-e.score, e.rel_path))

    lines: list[str] = ["REPO MAP (top files by symbol density):"]
    used = len(lines[0]) + 1
    truncated_count = 0

    for entry in entries_sorted:
        if entry.symbols:
            sym_str = ", ".join(f"{k} {n}" for k, n in entry.symbols[:8])
            if len(entry.symbols) > 8:
                sym_str += f", +{len(entry.symbols) - 8} more"
            line = f"  {entry.rel_path}: {sym_str}"
        else:
            line = f"  {entry.rel_path}"

        if used + len(line) + 1 > max_chars:
            truncated_count += 1
            continue

        lines.append(line)
        used += len(line) + 1

    if truncated_count:
        footer = f"  ... {truncated_count} more files omitted (budget exceeded)"
        if used + len(footer) + 1 <= max_chars:
            lines.append(footer)

    return "\n".join(lines)


def _safe_subdir(worktree_path: str, subdir: str | None) -> str | None:
    """Validate a drill-down subdir stays inside the worktree (RLM E4).

    Returns the cleaned relative subdir, or None when absent/invalid/escaping.
    """
    if not subdir:
        return None
    raw = subdir.strip()
    # Reject absolute and drive-relative paths on every OS / Python version.
    # (Py 3.13's ntpath.isabs no longer treats a leading-slash path as absolute,
    # so a bare isabs() check would let "/etc" through on Windows.)
    if os.path.isabs(raw) or raw.startswith(("/", "\\")):
        return None
    # Accept either separator on input; normalise to "/" for the returned value.
    sub = raw.replace("\\", "/").strip("/")
    if not sub:
        return None
    base = os.path.realpath(worktree_path)
    target = os.path.realpath(os.path.join(base, sub))
    if target != base and not target.startswith(base + os.sep):
        return None  # traversal attempt (.. escaping the worktree)
    return sub


def build_mermaid(
    root_path: str,
    max_depth: int = 2,
    max_nodes: int = 80,
) -> tuple[str, dict]:
    """Build a Mermaid ``flowchart TD`` of the repo's module tree.

    Deterministic, no LLM — pure directory walk reusing the same skip rules as
    the repo map. v1 emits the directory tree only (top ``max_depth`` levels,
    each node annotated with its direct code-file count); import-graph edges are
    intentionally deferred (too noisy from a regex index). Never raises — on any
    error it returns a one-node stub so callers can always render something.

    Returns (mermaid_text, stats_dict).
    """
    if not root_path or not os.path.isdir(root_path):
        return ('flowchart TD\n  n0["(repo not available)"]',
                {"nodes": 0, "error": True})
    try:
        base = os.path.realpath(root_path)
        # rel dir ("" = root) → count of direct code/important files
        dirs: dict[str, int] = {}
        for dirpath, dirnames, filenames in os.walk(base):
            # Prune skip dirs in place (same rule as the repo map scan).
            dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
            rel = os.path.relpath(dirpath, base).replace(os.sep, "/")
            if rel == ".":
                rel = ""
            depth = 0 if rel == "" else rel.count("/") + 1
            if depth > max_depth:
                dirnames[:] = []  # stop descending past the depth budget
                continue
            dirs[rel] = sum(
                1 for f in filenames
                if os.path.splitext(f)[1].lower() in EXT_LANG
                or f in IMPORTANT_FILENAMES
            )

        # Cap node count. Sorting by path keeps every parent ahead of its
        # children (a dir always sorts before its "dir/..." descendants), so a
        # truncation never orphans an included child from an included parent.
        rel_dirs = sorted(dirs.keys())
        truncated = len(rel_dirs) > max_nodes
        rel_dirs = rel_dirs[:max_nodes]
        included = set(rel_dirs)
        ids = {rel: f"n{i}" for i, rel in enumerate(rel_dirs)}

        repo_label = os.path.basename(base) or "repo"
        lines = ["flowchart TD"]
        for rel in rel_dirs:
            name = repo_label if rel == "" else rel.split("/")[-1]
            count = dirs[rel]
            label = name + (f"<br/>{count} files" if count else "")
            # Mermaid node labels can't contain raw double-quotes.
            label = label.replace('"', "'")
            lines.append(f'  {ids[rel]}["{label}"]')

        edges = 0
        for rel in rel_dirs:
            if rel == "":
                continue
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if parent in included:
                lines.append(f"  {ids[parent]} --> {ids[rel]}")
                edges += 1

        return "\n".join(lines), {
            "nodes": len(rel_dirs),
            "edges": edges,
            "truncated": truncated,
        }
    except Exception:
        logger.exception("build_mermaid failed for %s", root_path)
        return ('flowchart TD\n  n0["(diagram unavailable)"]',
                {"nodes": 0, "error": True})


def build_repo_map(
    worktree_path: str,
    max_files: int = 500,
    max_chars: int = DEFAULT_MAX_CHARS,
    subdir: str | None = None,
) -> tuple[str, dict]:
    """Build a repo map for the given worktree.

    ``subdir`` (RLM E4) scopes the scan to a sub-tree for a deeper map of the
    area the agent is editing; it is path-traversal–guarded.

    Returns (rendered_text, stats_dict). Both are always returned — on any
    internal error we return a stub map and log the exception so the task
    can still proceed.
    """
    safe_sub = _safe_subdir(worktree_path, subdir)
    try:
        entries = _scan_worktree(worktree_path, max_files=max_files, subdir=safe_sub)
    except Exception:
        logger.exception("repo_map scan failed for %s", worktree_path)
        return ("(repo map unavailable: scan error)", {"files": 0, "symbols": 0, "error": True})

    total_symbols = sum(len(e.symbols) for e in entries)
    rendered = _render(entries, max_chars=max_chars)
    stats = {
        "files": len(entries),
        "symbols": total_symbols,
        "chars": len(rendered),
        "truncated": len(rendered) >= max_chars - 100,
    }
    logger.info(
        "repo_map built for %s: %d files, %d symbols, %d chars",
        worktree_path, len(entries), total_symbols, len(rendered),
    )
    return rendered, stats
