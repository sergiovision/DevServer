"""Reality gate (lite) — pre-execution evidence scan for a coding task.

Before an agent starts building, scan independent evidence sources in
parallel, compute a weighted 0-100 "reality signal" with an explainable
evidence chain, and degrade gracefully when any source is unavailable.

The signal is advisory, not gating — it is injected into the agent prompt so
the agent has context about the *state of the world* before it starts editing
files. A low score does NOT block execution by itself; the opt-in
``reality_abstain_threshold`` setting can turn it into a hard abstain.

Sources scanned (both optional, run in parallel):
    1. Repo map hit-rate   — do the symbols / files the task mentions exist?
    2. Recent-commit scan  — have those files been touched in the last 14 days?

Output format:
    {
        "score": 0..100,                  # higher = more grounded in real evidence
        "confidence": "low|medium",
        "evidence": [                     # per-source evidence entries
            {"source": "repo_map", "weight": 0.67, "ok": True, "signal": ..., "note": "..."},
            ...
        ],
        "warnings": ["..."],              # human-readable concerns
        "degraded_sources": ["..."],      # sources that failed and were dropped
    }
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from services import proc as proc_util

logger = logging.getLogger(__name__)

# Weights sum to 1.0. When a source fails, its weight is redistributed across
# the remaining sources proportionally — graceful degradation.
BASE_WEIGHTS = {
    "repo_map": 0.65,
    "recent_commits": 0.35,
}

# How far back we consider a file "recently touched".
RECENT_COMMIT_WINDOW_DAYS = 14

# Minimum token length for a word we'll try to match against the repo map.
MIN_TOKEN_LEN = 4


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tokenize_task(title: str, description: str, acceptance: str) -> set[str]:
    """Extract interesting tokens (symbols, file hints, keywords) from a task.

    We keep this dumb on purpose: anything CamelCase, snake_case, kebab-case,
    or that looks like a file path / extension. No NLP, no LLM calls.
    """
    text = " ".join([title or "", description or "", acceptance or ""])

    tokens: set[str] = set()

    # CamelCase, snake_case, kebab-case identifiers.
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_\-/.]+", text):
        tok = m.group(0)
        if len(tok) >= MIN_TOKEN_LEN and not tok.isdigit():
            tokens.add(tok)

    # Anything that looks like a file path (has a slash and a dot).
    for m in re.finditer(r"[\w\-./]+\.\w{1,6}", text):
        tokens.add(m.group(0))

    return tokens


def _redistribute_weights(active: set[str]) -> dict[str, float]:
    """Renormalize BASE_WEIGHTS over the set of sources that succeeded."""
    if not active:
        return {}
    total = sum(BASE_WEIGHTS.get(k, 0) for k in active)
    if total <= 0:
        return {}
    return {k: BASE_WEIGHTS[k] / total for k in active}


# ── Source 1: repo-map hit rate ───────────────────────────────────────────────

async def _check_repo_map(
    repo_map_text: str,
    task_tokens: set[str],
) -> dict[str, Any]:
    """How many of the task's tokens actually appear in the repo map?

    High hit rate = the task is talking about things that exist.
    Low hit rate = the task may be hallucinating file/symbol names,
    which is exactly when Claude is most likely to go off the rails.
    """
    if not task_tokens:
        return {
            "source": "repo_map",
            "ok": True,
            "signal": 0.5,   # neutral — we just don't know
            "note": "no meaningful tokens in task text",
        }

    if not repo_map_text or repo_map_text.startswith("(repo map"):
        return {
            "source": "repo_map",
            "ok": False,
            "signal": 0.0,
            "note": "repo map unavailable",
        }

    lowered = repo_map_text.lower()
    hits = 0
    for tok in task_tokens:
        if tok.lower() in lowered:
            hits += 1

    hit_rate = hits / len(task_tokens)
    note = f"{hits}/{len(task_tokens)} task tokens matched ({hit_rate:.0%})"
    return {
        "source": "repo_map",
        "ok": True,
        "signal": hit_rate,
        "note": note,
        "hits": hits,
        "total": len(task_tokens),
    }


# ── Source 2: recent-commit scan ──────────────────────────────────────────────

async def _check_recent_commits(
    worktree_path: str,
    task_tokens: set[str],
) -> dict[str, Any]:
    """Are files related to this task currently hot (recently modified)?

    Warm files are more likely to have unreviewed state; very cold files
    are usually safer to touch. This is a weak signal on its own but it
    helps the agent know 'someone else was just here'.
    """
    if not worktree_path or not os.path.isdir(worktree_path):
        return {
            "source": "recent_commits",
            "ok": False,
            "signal": 0.0,
            "note": "no worktree",
        }

    try:
        _, stdout, _ = await proc_util.run(
            [
                "git", "-C", worktree_path, "log",
                f"--since={RECENT_COMMIT_WINDOW_DAYS} days ago",
                "--name-only", "--pretty=format:", "-n", "200",
            ],
            timeout=10,
            stderr_devnull=True,
        )
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return {
            "source": "recent_commits",
            "ok": False,
            "signal": 0.0,
            "note": "git log failed",
        }

    recent_files = {
        line.strip() for line in stdout.decode(errors="replace").splitlines()
        if line.strip()
    }

    if not recent_files:
        return {
            "source": "recent_commits",
            "ok": True,
            "signal": 0.6,  # neutral-positive: quiet repo → safer
            "note": "no commits in last 14 days",
            "recent_files": 0,
        }

    # Count collisions: recent files whose path contains a task token.
    collisions = 0
    sample: list[str] = []
    for rf in recent_files:
        rf_lower = rf.lower()
        for tok in task_tokens:
            if len(tok) >= 4 and tok.lower() in rf_lower:
                collisions += 1
                if len(sample) < 3:
                    sample.append(rf)
                break

    # Signal: some overlap is fine; heavy overlap means active work elsewhere.
    if collisions == 0:
        signal = 0.9  # very clean
        note = f"{len(recent_files)} recent files, none overlap task"
    elif collisions <= 3:
        signal = 0.65
        note = f"{collisions} recent files overlap: {', '.join(sample)}"
    else:
        signal = 0.35
        note = f"{collisions} recent files overlap — active area: {', '.join(sample)}"

    return {
        "source": "recent_commits",
        "ok": True,
        "signal": signal,
        "note": note,
        "recent_files": len(recent_files),
        "collisions": collisions,
    }


# ── Public API ────────────────────────────────────────────────────────────────

async def run_reality_gate(
    *,
    worktree_path: str,
    repo_map_text: str,
    title: str,
    description: str,
    acceptance: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run all gate sources in parallel and compute a weighted signal.

    Never raises. Returns a structured dict even on total failure.
    """
    task_tokens = _tokenize_task(title, description, acceptance)

    results = await asyncio.gather(
        _check_repo_map(repo_map_text, task_tokens),
        _check_recent_commits(worktree_path, task_tokens),
        return_exceptions=True,
    )

    evidence: list[dict[str, Any]] = []
    degraded: list[str] = []
    active: set[str] = set()

    for r in results:
        if isinstance(r, BaseException):
            logger.debug("reality gate source raised: %s", r)
            continue
        evidence.append(r)
        if r.get("ok"):
            active.add(r["source"])
        else:
            degraded.append(r["source"])

    weights = _redistribute_weights(active)

    # Weighted sum over active sources → 0..1, then * 100.
    score_01 = 0.0
    for ev in evidence:
        src = ev["source"]
        if src in weights:
            ev["weight"] = round(weights[src], 3)
            score_01 += weights[src] * float(ev.get("signal", 0))
        else:
            ev["weight"] = 0.0

    score = round(score_01 * 100)
    confidence = "medium" if len(active) >= 2 else "low"

    warnings: list[str] = []
    for ev in evidence:
        if ev.get("source") == "repo_map" and ev.get("ok") and float(ev.get("signal", 1)) < 0.25:
            warnings.append(
                "very few task tokens were found in the repo map — "
                "the task may be referring to code that doesn't exist"
            )
        if ev.get("source") == "recent_commits" and ev.get("ok") and float(ev.get("signal", 1)) < 0.5:
            warnings.append(ev.get("note", "task overlaps an actively changing area"))

    return {
        "score": score,
        "confidence": confidence,
        "evidence": evidence,
        "warnings": warnings,
        "degraded_sources": degraded,
        "task_tokens": sorted(task_tokens)[:30],
    }


def render_for_prompt(signal: dict[str, Any]) -> str:
    """Render the signal dict as a compact block for inclusion in the prompt."""
    if not signal:
        return ""

    score = signal.get("score", "?")
    confidence = signal.get("confidence", "?")
    lines = [
        f"## Reality Signal: {score}/100 (confidence: {confidence})",
        "This is a pre-execution evidence scan — use it to calibrate your approach.",
    ]

    for ev in signal.get("evidence", []):
        src = ev.get("source", "?")
        weight = ev.get("weight", 0)
        note = ev.get("note", "")
        status = "ok" if ev.get("ok") else "unavailable"
        lines.append(f"- [{src} w={weight} {status}] {note}")

    warnings = signal.get("warnings", [])
    if warnings:
        lines.append("")
        lines.append("WARNINGS:")
        for w in warnings:
            lines.append(f"- {w}")

    degraded = signal.get("degraded_sources", [])
    if degraded:
        lines.append("")
        lines.append(f"(degraded sources: {', '.join(degraded)} — their weight was redistributed)")

    return "\n".join(lines)
