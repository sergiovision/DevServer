"""Side-effect gate classifier — decide whether a pending agent action needs
human approval before it runs.

Structurally a twin of ``error_classifier.py``: a list of regex ``_Rule``s
maps an action description to a :class:`GateClass` with a ``kind`` and a
``severity``. The hard rule from ``PlanImprove.md`` §C:

    ALWAYS blocking — spend_money, send_message, publish, clinical, legal,
                      irreversible.
    Non-blocking    — gather/scrape, draft, analyse, score, schedule reminders.
    Ambiguous       — defaults to BLOCKING (fail-safe).

``classify()`` is deterministic and cheap. ``classify_with_llm()`` adds an
optional system-LLM fallback for genuinely ambiguous text (used only when the
regex pass returns the ``ambiguous`` catch-all and a session is available).

This module is FREE-core — no Pro imports. The suspend/resume state machine
that consumes a :class:`GateClass` lives in ``services/side_effect_gate.py``.
"""

from __future__ import annotations

import json as _json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: kinds that are always blocking (mandatory human approval).
BLOCKING_KINDS = frozenset(
    {"spend_money", "send_message", "publish", "clinical", "legal", "irreversible"}
)


@dataclass
class GateClass:
    kind: str        # one of the decision_points.kind values
    severity: str    # 'blocking' | 'non_blocking'
    hint: str        # short human-readable explanation of why
    matched: str = ""  # the rule key that matched (for logging/telemetry)


@dataclass
class _Rule:
    key: str          # decision_points.kind to assign
    pattern: re.Pattern[str]
    hint: str


# ── Rules table ───────────────────────────────────────────────────────────────
# Order matters: earlier rules win. Most dangerous / specific first. Every
# kind here is in BLOCKING_KINDS, so any match means "blocking". The final
# step is a non-blocking allow-list; anything that matches neither is
# 'ambiguous' → blocking (fail-safe).

_BLOCKING_RULES: list[_Rule] = [
    # ─── spend money ───
    _Rule("spend_money",
          re.compile(r"\b(pay|payment|purchase|buy|checkout|charge|wire|"
                     r"transfer\s+(?:money|funds)|invoice|subscribe|"
                     r"ad\s*spend|budget|deposit|refund|payout)\b", re.I),
          "Spends money or moves funds."),
    _Rule("spend_money",
          re.compile(r"\b(stripe|paypal|credit\s*card|bank\s*account|crypto|"
                     r"\$\d|usd\s*\d|wire\s*transfer)\b", re.I),
          "Touches a payment instrument or amount."),

    # ─── send a message externally ───
    _Rule("send_message",
          re.compile(r"\b(send|submit|post|dm|reply|respond)\b.{0,40}\b"
                     r"(email|e-mail|message|application|cover\s*letter|"
                     r"outreach|connection\s*request|sms|text|dm)\b", re.I),
          "Sends an external message / submits an application."),
    _Rule("send_message",
          re.compile(r"\b(smtp|sendgrid|mailgun|twilio|gmail\s*api|"
                     r"slack\s*webhook|telegram\s*send|linkedin\s*(?:message|connect))\b", re.I),
          "Uses an external messaging/outreach channel."),

    # ─── publish ───
    _Rule("publish",
          re.compile(r"\b(publish|go\s*live|deploy\s*to\s*prod|release|"
                     r"merge\s+(?:the\s+)?pr|launch\s+campaign|schedule\s+post|"
                     r"make\s+public)\b", re.I),
          "Publishes or makes content/changes public."),

    # ─── clinical ───
    _Rule("clinical",
          re.compile(r"\b(dose|dosage|prescri|diagnos|treatment\s+plan|"
                     r"medication|supplement\s+regimen|mg\b|titrat)\b", re.I),
          "Makes a medical/clinical decision — must defer to a clinician."),

    # ─── legal ───
    _Rule("legal",
          re.compile(r"\b(sign\b|signature|contract|power\s*of\s*attorney|"
                     r"legal\s*filing|notarize|deed|settlement\s+agreement|"
                     r"e-?sign)\b", re.I),
          "Signs or commits to a legal instrument."),

    # ─── irreversible ───
    _Rule("irreversible",
          re.compile(r"\b(rm\s+-rf|drop\s+(?:table|database)|delete\s+"
                     r"(?:all|production|the\s+repo|account)|force\s*push|"
                     r"truncate\b|deed\s*transfer|wipe|destroy)\b", re.I),
          "Irreversible deletion / destructive action."),
]

# Clearly safe, non-blocking actions. If one of these matches AND no blocking
# rule did, the action is allowed without a gate.
_NON_BLOCKING = re.compile(
    r"\b(gather|scrape|fetch|read|search|research|analy[sz]e|summari[sz]e|"
    r"draft|score|rank|chart|track|compute|generate\s+(?:a\s+)?(?:report|draft|summary)|"
    r"set\s+a?\s*reminder|save\s+(?:to\s+)?(?:disk|file|db|database))\b",
    re.I,
)


def classify(action: str) -> GateClass:
    """Classify a pending action. Never returns None — unknown ⇒ blocking.

    ``action`` is a short free-text description of what the agent is about to
    do (plus any URL/recipient/amount in it).
    """
    text = (action or "").strip()
    if not text:
        # No description ⇒ we cannot reason about it ⇒ fail safe.
        return GateClass("ambiguous", "blocking",
                         "Empty action description — blocking as a fail-safe.",
                         matched="empty")

    for rule in _BLOCKING_RULES:
        if rule.pattern.search(text):
            return GateClass(rule.key, "blocking", rule.hint, matched=rule.key)

    if _NON_BLOCKING.search(text):
        return GateClass("ambiguous", "non_blocking",
                         "Recognised as a read/draft/analysis action.",
                         matched="allow_list")

    # Matched nothing → ambiguous → blocking (fail-safe).
    return GateClass("ambiguous", "blocking",
                     "Unrecognised action — blocking as a fail-safe; "
                     "approve if it is a safe read/draft action.",
                     matched="default")


_LLM_PROMPT = """You are a safety gate for an autonomous agent. Decide whether \
the following pending action requires human approval before it runs.

ALWAYS require approval (blocking) if it: spends money, sends an external \
message/application, publishes anything, makes a clinical/medical decision, \
signs/commits legally, or is irreversible (deletes data, transfers a deed).
Do NOT require approval (non_blocking) for: gathering/reading public data, \
drafting, internal analysis, scoring, producing artifacts, setting reminders.
If you are unsure, choose blocking.

Pending action:
{action}

Reply with ONLY a JSON object:
{{"kind": "spend_money|send_message|publish|clinical|legal|irreversible|ambiguous",
  "severity": "blocking|non_blocking", "hint": "<one line>"}}"""


async def classify_with_llm(action: str, *, vendor: str, model: str) -> GateClass:
    """Regex first; only consult the LLM when the regex result is the
    ``ambiguous`` catch-all (either non-blocking allow-list or default block).

    Any LLM/parse error falls back to the deterministic regex verdict, so this
    is always safe to call.
    """
    base = classify(action)
    if base.kind != "ambiguous":
        return base  # a concrete blocking kind matched — trust it, skip the LLM.

    # Lazy import to keep this module free of heavy deps at import time.
    from services import llm_client

    try:
        raw = await llm_client.complete(
            vendor=vendor, model=model, prompt=_LLM_PROMPT.format(action=action[:2000]),
            max_tokens=256, timeout=45,
        )
        cleaned = raw.strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        obj = _json.loads(m.group(0)) if m else None
        if isinstance(obj, dict):
            kind = str(obj.get("kind") or "ambiguous")
            severity = "blocking" if str(obj.get("severity")) != "non_blocking" else "non_blocking"
            hint = str(obj.get("hint") or base.hint)
            return GateClass(kind, severity, hint, matched="llm")
    except Exception:
        logger.exception("gate LLM fallback failed; using regex verdict")
    return base
