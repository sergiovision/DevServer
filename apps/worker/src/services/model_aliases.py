"""Floating model aliases: ``claude-opus-latest``, ``gemini-flash-latest``, ``gpt-latest``.

A task, a repo default, the backup model or the System LLM may store an alias
instead of a concrete slug. The alias is mapped onto the newest matching model
of the registry at the moment the CLI (or HTTP call) is built. "Newest" is
decided by the version parsed out of the slug, never by catalogue order, so
adding ``claude-opus-6`` anywhere in the registry is the whole upgrade: every
row holding ``claude-opus-latest`` follows it without being edited.

This module is pure — it knows slug grammar and alias names, not the registry.
``services.agent_backends`` binds it to ``VENDOR_MODELS`` (``resolve_model``)
and publishes the aliases to the dashboard through ``/internal/agent-registry``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

LATEST_SUFFIX = "-latest"
#: ``-last`` is accepted as a spelling of ``-latest`` ("claude-opus-last").
_LAST_SUFFIX = "-last"


@dataclass(frozen=True)
class AliasSpec:
    """A family selector: the highest-versioned matching slug wins."""

    id: str
    label: str
    #: '' = any family of the vendor.
    family: str = ""
    #: Gemini only (the effort is part of the slug); '' = any.
    effort: str = ""


@dataclass(frozen=True)
class ModelAlias:
    """One picker row: an alias and the concrete model it names today."""

    id: str
    label: str
    resolves_to: str


ALIAS_SPECS: dict[str, tuple[AliasSpec, ...]] = {
    "anthropic": (
        AliasSpec("claude-opus-latest", "Claude Opus — latest", "opus"),
        AliasSpec("claude-sonnet-latest", "Claude Sonnet — latest", "sonnet"),
        AliasSpec("claude-haiku-latest", "Claude Haiku — latest", "haiku"),
        AliasSpec("claude-fable-latest", "Claude Fable — latest", "fable"),
    ),
    "google": (
        *(
            AliasSpec(
                f"gemini-{family}{infix}-latest",
                f"Gemini {family.title()} ({effort.title()}) — latest",
                family,
                effort,
            )
            for family in ("argon", "flash")
            # The bare alias is the High tier, as in ``gemini-argon-latest``.
            for effort, infix in (("high", ""), ("medium", "-medium"), ("low", "-low"))
        ),
    ),
    "openai": (
        AliasSpec("gpt-latest", "GPT — latest"),
        AliasSpec("gpt-astra-latest", "GPT Astra — latest", "astra"),
        AliasSpec("gpt-sol-latest", "GPT Sol — latest", "sol"),
        AliasSpec("gpt-terra-latest", "GPT Terra — latest", "terra"),
        AliasSpec("gpt-luna-latest", "GPT Luna — latest", "luna"),
    ),
}

#: The vendor prefix may be left off ("opus-latest").
SHORT_ALIASES: dict[str, str] = {
    "opus-latest": "claude-opus-latest",
    "sonnet-latest": "claude-sonnet-latest",
    "haiku-latest": "claude-haiku-latest",
    "fable-latest": "claude-fable-latest",
    "claude-latest": "claude-opus-latest",
    "gemini-latest": "gemini-flash-latest",
    "argon-latest": "gemini-argon-latest",
    # agy no longer lists a Gemini Pro; the Pro aliases follow Argon, the
    # deep-reasoning line that replaced it.
    "gemini-pro-latest": "gemini-argon-latest",
    "gemini-pro-low-latest": "gemini-argon-low-latest",
}

# claude-opus-5-5, claude-opus-5, claude-haiku-4-5-20251001. The effort-suffixed
# slugs Antigravity serves (claude-opus-5-5-high) do not match on purpose.
_CLAUDE_SLUG = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$")
# gemini-3.8-flash-high
_GEMINI_SLUG = re.compile(r"^gemini-(\d+(?:\.\d+)*)-([a-z]+)-(low|medium|high)$")
# gpt-6-astra, gpt-5.6-sol, gpt-5.5
_GPT_SLUG = re.compile(r"^gpt-(\d+(?:\.\d+)*)(?:-([a-z]+))?$")


@dataclass(frozen=True)
class _ParsedSlug:
    family: str
    effort: str
    version: tuple[int, ...]


def _dotted(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _parse_slug(vendor: str, model_id: str) -> _ParsedSlug | None:
    if vendor == "anthropic" and (m := _CLAUDE_SLUG.match(model_id)):
        return _ParsedSlug(m[1], "", (int(m[2]), int(m[3] or 0)))
    if vendor == "google" and (m := _GEMINI_SLUG.match(model_id)):
        return _ParsedSlug(m[2], m[3], _dotted(m[1]))
    if vendor == "openai" and (m := _GPT_SLUG.match(model_id)):
        return _ParsedSlug(m[2] or "", "", _dotted(m[1]))
    return None


def _version_key(version: tuple[int, ...]) -> tuple[int, ...]:
    """Pad so ``6`` and ``6.0`` compare equal and ``6.1`` beats ``6``."""
    return version + (0,) * (4 - len(version))


def _normalise(model: str) -> str:
    key = model.strip().lower()
    if key.endswith(_LAST_SUFFIX):
        key = key.removesuffix(_LAST_SUFFIX) + LATEST_SUFFIX
    return SHORT_ALIASES.get(key, key)


def is_latest_alias(model: str) -> bool:
    """True when ``model`` is spelled like a floating alias (any vendor)."""
    key = (model or "").strip().lower()
    return key.endswith((LATEST_SUFFIX, _LAST_SUFFIX))


def _resolve_spec(vendor: str, spec: AliasSpec, catalogue: Iterable[str]) -> str:
    """Highest-versioned slug the spec selects; a tie keeps the earlier entry."""
    best, best_key = "", None
    for model_id in catalogue:
        parsed = _parse_slug(vendor, model_id)
        if parsed is None:
            continue
        if spec.family and parsed.family != spec.family:
            continue
        if spec.effort and parsed.effort != spec.effort:
            continue
        key = _version_key(parsed.version)
        if best_key is None or key > best_key:
            best, best_key = model_id, key
    return best


def resolve(vendor: str, model: str, catalogue: Iterable[str]) -> str:
    """Map a floating alias onto the concrete model it names in ``catalogue``.

    Anything else — a concrete slug, a free-form model string, an alias the
    vendor does not know, or one with no candidate — is returned unchanged, so
    the CLI still reports its own error for a genuinely unknown model.
    """
    if not is_latest_alias(model):
        return model
    key = _normalise(model)
    for spec in ALIAS_SPECS.get(vendor, ()):
        if spec.id == key:
            return _resolve_spec(vendor, spec, catalogue) or model
    return model


def latest_aliases(vendor: str, catalogue: Iterable[str]) -> tuple[ModelAlias, ...]:
    """A vendor's aliases with their current target; unresolvable ones are omitted."""
    ids = tuple(catalogue)
    resolved = ((spec, _resolve_spec(vendor, spec, ids)) for spec in ALIAS_SPECS.get(vendor, ()))
    return tuple(ModelAlias(spec.id, spec.label, target) for spec, target in resolved if target)
