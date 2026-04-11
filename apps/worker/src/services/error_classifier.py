"""Error classifier — turn raw verifier output into targeted remediation hints.

DevServer's retry loop today feeds the last 3000 chars of stderr back into
the prompt and hopes Claude figures it out. That burns a full Claude session
per retry even on trivial errors. This module parses the output with small
language/tool-specific patterns and produces:

    - a normalized error *class*  (e.g. "python.import_error", "dotnet.build_error")
    - a short, surgical *hint*    (e.g. "File X imports Y which does not exist; "
                                   "check the module name or add a dependency")
    - the most relevant *context*  (up to a few matched lines)

The classification is intentionally coarse — we want enough signal to either
(a) inject a structured nudge into the resume prompt, or (b) escalate to
'blocked' when the same class repeats.

All patterns are regex-based, no external deps. New languages/tools can be
added by appending to ``RULES``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class ErrorClass:
    key: str                  # e.g. "python.import_error"
    hint: str                 # short structured remediation hint
    evidence: list[str] = field(default_factory=list)  # matched lines (for log)
    severity: str = "recoverable"  # "recoverable" | "hard"


# ── Rule definition helpers ───────────────────────────────────────────────────

@dataclass
class _Rule:
    key: str
    pattern: re.Pattern[str]
    hint_template: str
    severity: str = "recoverable"
    extract: Callable[[re.Match[str]], dict[str, str]] | None = None


def _simple_extract(m: re.Match[str]) -> dict[str, str]:
    return {f"g{i}": g for i, g in enumerate(m.groups() or ()) if g}


# ── Rules table ───────────────────────────────────────────────────────────────
# Order matters: earlier rules win. Keep the most specific/dramatic first.

RULES: list[_Rule] = [
    # ─── Python ───
    _Rule(
        key="python.syntax_error",
        pattern=re.compile(r"^\s*File \"([^\"]+)\", line (\d+)\s*\n.*?\n\s*SyntaxError: (.+)$", re.M),
        hint_template="Python SyntaxError in {g0} line {g1}: {g2}. Fix the exact line; do not rewrite the file.",
    ),
    _Rule(
        key="python.import_error",
        pattern=re.compile(r"(?:ModuleNotFoundError|ImportError): (?:No module named )?['\"]?([^'\"\n]+?)['\"]?$", re.M),
        hint_template="Python cannot import '{g0}'. Either the module name is wrong, "
                      "or the dependency is missing from pyproject.toml/requirements. "
                      "Verify the exact module path by reading imports in related files.",
    ),
    _Rule(
        key="python.name_error",
        pattern=re.compile(r"NameError: name ['\"]([^'\"]+)['\"] is not defined", re.M),
        hint_template="Python NameError: '{g0}' is not defined. Likely a missing import or a typo. "
                      "Read the file and find where {g0} should come from.",
    ),
    _Rule(
        key="python.attribute_error",
        pattern=re.compile(r"AttributeError: (?:module ['\"][^'\"]+['\"] has no attribute|['\"][^'\"]+['\"] object has no attribute) ['\"]([^'\"]+)['\"]", re.M),
        hint_template="Python AttributeError on '{g0}'. The attribute does not exist — "
                      "inspect the actual class/module definition before using it.",
    ),
    _Rule(
        key="python.type_error",
        pattern=re.compile(r"TypeError: (.+)$", re.M),
        hint_template="Python TypeError: {g0}. Check argument types and call signatures.",
    ),
    _Rule(
        key="python.pytest_failure",
        pattern=re.compile(r"FAILED (\S+::\S+) - (.+)$", re.M),
        hint_template="pytest failure in {g0}: {g1}. Read the failing test first, then fix the code — do not edit the test unless the task explicitly says so.",
    ),

    # ─── Node / TypeScript ───
    _Rule(
        key="ts.compile_error",
        pattern=re.compile(r"^([^(:\n]+\.tsx?)\((\d+),(\d+)\): error (TS\d+): (.+)$", re.M),
        hint_template="TypeScript {g3} at {g0}:{g1}:{g2}: {g4}. Fix the exact diagnostic; don't disable the check.",
    ),
    _Rule(
        key="ts.cannot_find_module",
        pattern=re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]", re.M),
        hint_template="TypeScript cannot resolve module '{g0}'. "
                      "Check tsconfig paths, verify the import specifier, or add the dep in package.json.",
    ),
    _Rule(
        key="node.module_not_found",
        pattern=re.compile(r"Error: Cannot find module ['\"]([^'\"]+)['\"]", re.M),
        hint_template="Node.js cannot find module '{g0}'. Run the project's install step or correct the import path.",
    ),
    _Rule(
        key="jest.test_failure",
        pattern=re.compile(r"●\s+(.+?)\n\s+(?:✕|●)?\s*(.+?)\n", re.M),
        hint_template="Jest test failure: {g0} — {g1}. Read the test expectation before changing code.",
    ),
    _Rule(
        key="eslint.error",
        pattern=re.compile(r"^\s*\d+:\d+\s+error\s+(.+?)\s{2,}([\w/-]+)$", re.M),
        hint_template="ESLint error: {g0} [{g1}]. Fix the rule violation at the reported location.",
    ),

    # ─── .NET / C# ───
    _Rule(
        key="dotnet.build_error",
        pattern=re.compile(r"([^(\s:]+)\((\d+),(\d+)\): error (CS\d+): (.+?) \[", re.M),
        hint_template="C# {g3} in {g0} ({g1},{g2}): {g4}. Fix the exact diagnostic before rebuilding.",
    ),
    _Rule(
        key="dotnet.missing_type",
        pattern=re.compile(r"error CS0246: The type or namespace name '([^']+)' could not be found", re.M),
        hint_template="C# cannot find type/namespace '{g0}'. Verify the using directive and project references.",
    ),
    _Rule(
        key="dotnet.test_failure",
        pattern=re.compile(r"^\s*Failed\s+(.+?)\s+\[", re.M),
        hint_template="Test failure in {g0}. Read the assertion message before modifying the tested code.",
    ),

    # ─── Rust ───
    _Rule(
        key="rust.compile_error",
        pattern=re.compile(r"^error\[E(\d+)\]: (.+)$", re.M),
        hint_template="Rust compile error E{g0}: {g1}. Fix the exact diagnostic — rustc spans are precise.",
    ),

    # ─── Go ───
    _Rule(
        key="go.undefined",
        pattern=re.compile(r"^([^:]+):(\d+):(\d+): undefined: (\w+)", re.M),
        hint_template="Go: undefined symbol '{g3}' at {g0}:{g1}. Add the missing import or declaration.",
    ),

    # ─── Anthropic API ───
    _Rule(
        key="api.rate_limit",
        pattern=re.compile(r"rate_limit_error|rate limit of \d+ (input )?tokens per minute", re.I),
        hint_template="Anthropic API rate limit hit. The task will pause and retry automatically.",
        severity="recoverable",
    ),

    # ─── Git ───
    _Rule(
        key="git.merge_conflict",
        pattern=re.compile(r"CONFLICT \(.+?\): (.+)", re.M),
        hint_template="Git merge conflict: {g0}. Resolve the conflict markers before continuing.",
        severity="hard",
    ),
    _Rule(
        key="git.nothing_to_commit",
        pattern=re.compile(r"nothing to commit", re.I),
        hint_template="Git reports 'nothing to commit' — you produced no file changes in the previous attempt. "
                      "Re-read the task and make the actual code edits.",
        severity="hard",
    ),

    # ─── Shell / generic ───
    _Rule(
        key="shell.command_not_found",
        pattern=re.compile(r"^([\w/.-]+): (?:command not found|not found)$", re.M),
        hint_template="Shell command '{g0}' was not found. Either the tool is not installed or the PATH is wrong — do not try to invoke it again without checking availability.",
        severity="hard",
    ),
    _Rule(
        key="permission_denied",
        pattern=re.compile(r"Permission denied", re.I),
        hint_template="Permission denied. Avoid touching the failing path; it is likely outside the worktree or read-only.",
        severity="hard",
    ),
    _Rule(
        key="timeout",
        pattern=re.compile(r"timed out after \d+s", re.I),
        hint_template="A verification step timed out. Investigate performance, not correctness — look for infinite loops or network calls.",
    ),
]


def classify(raw: str) -> ErrorClass | None:
    """Find the first matching rule and return an ErrorClass, or None."""
    if not raw:
        return None

    # Cap the window we scan — error output can be huge.
    window = raw[-12000:] if len(raw) > 12000 else raw

    for rule in RULES:
        match = rule.pattern.search(window)
        if not match:
            continue

        extras = _simple_extract(match) if rule.extract is None else rule.extract(match)
        try:
            hint = rule.hint_template.format(**extras)
        except (KeyError, IndexError):
            hint = rule.hint_template

        # Evidence = the matched line plus one line of surrounding context.
        start = max(0, match.start() - 80)
        end = min(len(window), match.end() + 80)
        evidence_line = window[start:end].strip().replace("\n", " | ")[:300]

        return ErrorClass(
            key=rule.key,
            hint=hint,
            evidence=[evidence_line],
            severity=rule.severity,
        )

    return None


def build_remediation_block(cls: ErrorClass | None, raw_tail: str) -> str:
    """Build the text block to inject into the retry prompt.

    Uses the classifier if it matched, falls back to the raw tail otherwise.
    """
    if cls is None:
        return (
            "## Previous Attempt Failed\n"
            "No structured error class was identified. Raw tail of the failure:\n"
            f"{raw_tail[-2000:]}\n\n"
            "Read the failure carefully and fix the root cause. Do not retry blindly."
        )

    parts = [
        "## Previous Attempt Failed — Classified",
        f"Error class: `{cls.key}`  (severity: {cls.severity})",
        f"Targeted hint: {cls.hint}",
    ]
    if cls.evidence:
        parts.append("")
        parts.append("Evidence:")
        for line in cls.evidence:
            parts.append(f"  > {line}")
    parts.append("")
    parts.append("Apply the targeted hint above. Do not rewrite unrelated files. "
                 "Do not re-run the same approach that failed.")
    return "\n".join(parts)
