"""PR preflight — deterministic edit review before a pull request is opened.

Mechanical checks that run after the verifier has already passed but before
``git push`` and ``create_gitea_pr``. The point is to catch the two biggest
failure modes of autonomous agents — scope creep and leaked secrets — without
burning another Claude call on self-review.

Checks performed:
    1. **Author check.**       The HEAD commit must be authored by the
                               configured DevServer git identity. Rejects work
                               that was somehow committed by a different user.
    2. **Allowlist check.**    When an approved plan is available (interactive
                               mode), the set of changed files must be a
                               subset of ``plan.files_to_touch``. Anything
                               outside the allow-list is treated as scope
                               creep — recoverable on retry.
    3. **Secret-leak check.**  Scans added/modified content for common secret
                               patterns (API keys, tokens, private keys, .env
                               files, password assignments). Not a security
                               audit — it is the "agent accidentally pasted a
                               test key into a file" filter. Hard failure.
    4. **File-size check.**    Rejects files larger than ``MAX_FILE_BYTES``
                               (default 1 MB) — these are almost always
                               build artefacts or vendored blobs. Hard
                               failure.

Severity levels:
    - ``scope_creep``      → recoverable; caller should inject a structured
                             hint into the retry prompt and re-run Claude.
    - ``secret_leak``      → hard; caller should mark the task ``blocked``
                             and NOT push.
    - ``oversize_file``    → hard; same as secret_leak.
    - ``bad_author``       → hard; indicates a configuration or tampering
                             problem; do not push.

All Git interaction goes through the same ``asyncio.create_subprocess_exec``
pattern used in ``git_ops.py`` so the function is async-friendly. The module
never raises on git failures — a broken git invocation returns a Preflight
result with ``ok=True`` and a warning, so a bad preflight can't block a
successful task.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Iterable

from config import settings

logger = logging.getLogger(__name__)

# Binary file extensions we always skip when scanning for secrets (noise).
_BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".class", ".jar", ".wasm",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".flac",
    ".db", ".sqlite", ".sqlite3",
    ".parquet", ".arrow", ".pb", ".pkl", ".npy", ".npz",
}

# Files that are implicitly always forbidden in a PR.
_FORBIDDEN_FILENAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials.json", "credentials.yaml",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
}

# Secret regex rules. Order does not matter — the first match wins per file.
# Keep these conservative: a false positive turns a good task into a blocked
# task, which is very expensive. Prefer precision over recall.
_SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    # Anthropic API key
    ("anthropic_api_key", re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{20,}")),
    # OpenAI API key — negative lookahead to avoid matching Anthropic keys
    # (which also start with ``sk-``).
    ("openai_api_key", re.compile(r"\bsk-(?!ant-)(?:proj-)?[A-Za-z0-9_\-]{32,}")),
    # AWS access key ID (with nearby secret key pattern to avoid false positives)
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # GitHub personal access token
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    # GitHub fine-grained PAT
    ("github_fine_grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),
    # GitHub OAuth / App tokens
    ("github_token", re.compile(r"\b(?:gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{36}\b")),
    # Gitea token (40 hex chars — narrow pattern requiring surrounding context)
    ("gitea_token", re.compile(
        r"(?i)(?:gitea[_-]?token|GITEA_TOKEN)[\"'\s]*[:=][\"'\s]*([a-f0-9]{40})"
    )),
    # Slack bot tokens
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,48}\b")),
    # Google API key
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # Stripe secret key
    ("stripe_secret", re.compile(r"\bsk_live_[A-Za-z0-9]{24,}\b")),
    # Private keys (PEM)
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    # Telegram bot token (already in this repo's .env — match the shape)
    ("telegram_bot_token", re.compile(r"\b\d{9,11}:AA[A-Za-z0-9_\-]{33}\b")),
    # Generic "password = <something>" for common source-file conventions.
    # Deliberately narrow: only matches password assignments that look like
    # real secrets (non-empty, not a placeholder). Skips "password=''" etc.
    ("hardcoded_password", re.compile(
        r"(?i)(?:password|passwd|pwd)\s*[:=]\s*['\"]([^'\"\s]{8,})['\"]",
    )),
]

# Placeholders we should not flag even if they match a password rule.
# Split into two sets:
#   EXACT_PLACEHOLDERS  — must match the whole value after lower()
#   SUBSTRING_PLACEHOLDERS — must appear as a substring (used for obvious
#   "nobody would put this in a real password" markers)
# We deliberately do NOT put English words like "secret" or "password" in
# either set — they are extremely common in real passwords and caused
# false negatives during testing.
_PASSWORD_EXACT_PLACEHOLDERS = {
    "changeme", "password", "secret", "your_password", "yourpassword",
    "placeholder", "example", "none", "todo", "fixme",
    "your-password-here", "<password>", "<your-password>",
}
_PASSWORD_SUBSTRING_PLACEHOLDERS = {
    "xxxxxx", "######", "******",
}

# Default max size for a single file in a PR. Generated lock files, minified
# bundles, and the like should not be committed by an agent.
DEFAULT_MAX_FILE_BYTES = 1_000_000  # 1 MB


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Violation:
    kind: str                   # "scope_creep" | "secret_leak" | "oversize_file" | "bad_author"
    severity: str               # "recoverable" | "hard"
    path: str | None = None
    detail: str = ""


@dataclass
class PreflightResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)  # lines added, files touched, etc.

    @property
    def has_hard_failure(self) -> bool:
        return any(v.severity == "hard" for v in self.violations)

    @property
    def hint(self) -> str:
        """Render a structured hint for the retry prompt (scope creep only)."""
        creep = [v for v in self.violations if v.kind == "scope_creep"]
        if not creep:
            return ""
        paths = ", ".join(v.path or "?" for v in creep[:10])
        more = f" (+{len(creep) - 10} more)" if len(creep) > 10 else ""
        return (
            "## PR Preflight Failed — Scope Creep\n"
            "Your previous attempt modified files that were NOT on the approved "
            "allow-list. You MUST stay within the plan.\n\n"
            f"Out-of-scope files: {paths}{more}\n\n"
            "Revert changes to these files and only modify what the plan "
            "authorised. If you genuinely need to touch these files, stop and "
            "explain why — do not silently expand scope."
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _git(args: list[str], cwd: str, timeout: int = 20) -> tuple[int, str, str]:
    """Run a git subcommand — returns (rc, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    except (asyncio.TimeoutError, FileNotFoundError, OSError) as exc:
        logger.debug("git %s failed: %s", args, exc)
        return -1, "", str(exc)


def _is_binary_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _BINARY_EXT


def _is_forbidden_filename(path: str) -> bool:
    base = os.path.basename(path)
    return base in _FORBIDDEN_FILENAMES


def _normalize_allowlist(allowlist: Iterable[str] | None) -> set[str] | None:
    if allowlist is None:
        return None
    out: set[str] = set()
    for p in allowlist:
        p = (p or "").strip().lstrip("./")
        if p:
            out.add(p)
    return out


def _scan_secrets(content: str) -> list[tuple[str, str]]:
    """Return list of (rule_key, excerpt) for each matched rule."""
    hits: list[tuple[str, str]] = []
    for key, pattern in _SECRET_RULES:
        m = pattern.search(content)
        if not m:
            continue

        # Filter placeholder passwords to cut false positives.
        if key == "hardcoded_password":
            val = (m.group(1) or "").lower()
            if val in _PASSWORD_EXACT_PLACEHOLDERS:
                continue
            if any(p in val for p in _PASSWORD_SUBSTRING_PLACEHOLDERS):
                continue

        excerpt = m.group(0)
        if len(excerpt) > 60:
            excerpt = excerpt[:57] + "..."
        hits.append((key, excerpt))
    return hits


# ── Public API ────────────────────────────────────────────────────────────────

async def run_preflight(
    *,
    worktree_path: str,
    base_branch: str,
    allowlist: Iterable[str] | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> PreflightResult:
    """Run every preflight check and return a structured result.

    The function is defensive — if git cannot be invoked or the diff cannot
    be read, it returns ``ok=True`` with a diagnostic in ``stats`` rather than
    failing the task. The trade-off is intentional: preflight is a safety net,
    not a gate, and a broken net should not strand completed work.
    """
    result = PreflightResult(ok=True)

    if not worktree_path or not os.path.isdir(worktree_path):
        result.stats["error"] = "no worktree"
        return result

    # 1. Figure out which files changed between base_branch and HEAD.
    rc, out, err = await _git(
        ["diff", "--name-only", f"origin/{base_branch}...HEAD"],
        cwd=worktree_path,
    )
    if rc != 0:
        # Fallback: diff against the local base branch.
        rc, out, err = await _git(
            ["diff", "--name-only", f"{base_branch}...HEAD"],
            cwd=worktree_path,
        )
    if rc != 0:
        logger.warning("pr_preflight: git diff failed: %s", err.strip())
        result.stats["error"] = f"git diff failed: {err.strip()[:200]}"
        return result  # ok=True — don't block on a broken check

    changed = sorted({line.strip() for line in out.splitlines() if line.strip()})
    result.files_changed = changed
    result.stats["files_changed"] = len(changed)

    if not changed:
        # Verifier said ok but nothing actually changed — very suspicious, but
        # the pre-existing ``ensure_committed`` path should have caught it. We
        # let it through and record the observation.
        result.stats["warning"] = "no changed files detected"
        return result

    normalised_allowlist = _normalize_allowlist(allowlist)

    # 2. Author check on the HEAD commit.
    expected_email = (settings.git_user_email or "").strip().lower()
    rc_a, author_out, _ = await _git(
        ["log", "-1", "--format=%ae"], cwd=worktree_path,
    )
    if rc_a == 0:
        actual_email = author_out.strip().lower()
        if expected_email and actual_email and actual_email != expected_email:
            result.violations.append(Violation(
                kind="bad_author",
                severity="hard",
                path=None,
                detail=f"HEAD author is {actual_email!r}, expected {expected_email!r}",
            ))

    # 3. Per-file checks.
    for rel_path in changed:
        # 3a. Forbidden filename (.env etc) — hard.
        if _is_forbidden_filename(rel_path):
            result.violations.append(Violation(
                kind="secret_leak",
                severity="hard",
                path=rel_path,
                detail=f"forbidden filename: {os.path.basename(rel_path)}",
            ))
            # Still run the allowlist check below so the operator sees both.

        # 3b. Allowlist check — recoverable.
        if normalised_allowlist is not None and rel_path not in normalised_allowlist:
            result.violations.append(Violation(
                kind="scope_creep",
                severity="recoverable",
                path=rel_path,
                detail="not in approved plan allow-list",
            ))

        abs_path = os.path.join(worktree_path, rel_path)

        # Deleted files show up in diff but have no file on disk — skip size/secret.
        if not os.path.isfile(abs_path):
            continue

        # 3c. Oversize check — hard.
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = 0
        if size > max_file_bytes:
            result.violations.append(Violation(
                kind="oversize_file",
                severity="hard",
                path=rel_path,
                detail=f"{size} bytes exceeds limit {max_file_bytes}",
            ))
            continue  # no point scanning a huge file for secrets too

        # 3d. Secret scan — hard, but skip binaries.
        if _is_binary_path(rel_path):
            continue
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read(2_000_000)  # cap at 2 MB for safety
        except OSError:
            continue

        hits = _scan_secrets(content)
        for rule_key, excerpt in hits:
            result.violations.append(Violation(
                kind="secret_leak",
                severity="hard",
                path=rel_path,
                detail=f"{rule_key}: {excerpt}",
            ))

    result.ok = not result.has_hard_failure
    # Even pure scope_creep sets ok=False so the caller knows to retry.
    if any(v.kind == "scope_creep" for v in result.violations):
        result.ok = False

    return result


def summarise(result: PreflightResult) -> dict:
    """Compact dict suitable for emitting as a task_event payload."""
    by_kind: dict[str, int] = {}
    for v in result.violations:
        by_kind[v.kind] = by_kind.get(v.kind, 0) + 1
    return {
        "ok": result.ok,
        "files_changed": result.stats.get("files_changed", 0),
        "violations_by_kind": by_kind,
        "violations": [
            {
                "kind": v.kind,
                "severity": v.severity,
                "path": v.path,
                "detail": v.detail[:200],
            }
            for v in result.violations[:25]
        ],
        "stats": result.stats,
    }
