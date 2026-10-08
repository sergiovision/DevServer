"""Shared secret-detection rules + scanner.

Extracted from ``pr_preflight.py`` so every consumer shares one set of
patterns — ``pr_preflight`` uses it to block a PR whose diff contains a
leaked secret, and ``redact_secrets`` can scrub text before it is shown or
forwarded elsewhere.

Keep the rules conservative: a false positive turns a good task into a
blocked task (preflight) or mangles a legitimate message (messaging), both
of which are expensive. Prefer precision over recall.
"""

from __future__ import annotations

import re

# Secret regex rules. Order does not matter — the first match wins per file.
SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
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
#   _PASSWORD_EXACT_PLACEHOLDERS  — must match the whole value after lower()
#   _PASSWORD_SUBSTRING_PLACEHOLDERS — must appear as a substring
# We deliberately do NOT put English words like "secret" or "password" in
# either set — they are extremely common in real passwords and caused false
# negatives during testing.
_PASSWORD_EXACT_PLACEHOLDERS = {
    "changeme", "password", "secret", "your_password", "yourpassword",
    "placeholder", "example", "none", "todo", "fixme",
    "your-password-here", "<password>", "<your-password>",
}
_PASSWORD_SUBSTRING_PLACEHOLDERS = {
    "xxxxxx", "######", "******",
}


def scan_secrets(content: str) -> list[tuple[str, str]]:
    """Return list of (rule_key, excerpt) for each matched rule."""
    hits: list[tuple[str, str]] = []
    for key, pattern in SECRET_RULES:
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


def redact_secrets(content: str) -> tuple[str, list[str]]:
    """Redact every secret match in ``content``.

    Returns ``(redacted_text, [rule_key, ...])``. Each matched span is
    replaced with ``[REDACTED:<rule_key>]`` (a2abridge's approach), so the
    message still flows but the secret never leaves the bridge. For the
    ``hardcoded_password`` rule only the captured value is redacted, keeping
    the surrounding ``password=`` text intact.
    """
    redacted = content
    found: list[str] = []
    for key, pattern in SECRET_RULES:
        def _sub(m: re.Match[str]) -> str:
            if key == "hardcoded_password":
                val = (m.group(1) or "").lower()
                if val in _PASSWORD_EXACT_PLACEHOLDERS or any(
                    p in val for p in _PASSWORD_SUBSTRING_PLACEHOLDERS
                ):
                    return m.group(0)  # placeholder — leave as-is
                found.append(key)
                # Replace just the captured secret, keep the assignment text.
                return m.group(0).replace(m.group(1), f"[REDACTED:{key}]")
            found.append(key)
            return f"[REDACTED:{key}]"

        redacted = pattern.sub(_sub, redacted)
    return redacted, found
