"""``services.model_aliases``: floating ``-latest`` aliases over a catalogue."""

import pytest

from services import model_aliases
from services.model_aliases import resolve

CLAUDE = (
    "claude-sonnet-5-5",
    "claude-opus-5",
    "claude-opus-5-5",
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-8",
)
GEMINI = (
    "gemini-3.1-pro-high",
    "gemini-4-argon-high",
    "gemini-4-argon-low",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-low",
    "gemini-3.7-flash-low",
    "claude-opus-5-5-high",
    "gpt-oss-120b-medium",
)
GPT = ("gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-5.6-luna", "gpt-5.5")


class TestResolve:
    @pytest.mark.parametrize(
        ("vendor", "alias", "catalogue", "expected"),
        [
            ("anthropic", "claude-opus-latest", CLAUDE, "claude-opus-5-5"),
            ("anthropic", "claude-fable-latest", CLAUDE, "claude-fable-5-2"),
            ("anthropic", "claude-haiku-latest", CLAUDE, "claude-haiku-4-5-20251001"),
            ("google", "gemini-argon-latest", GEMINI, "gemini-4-argon-high"),
            ("google", "gemini-flash-low-latest", GEMINI, "gemini-3.8-flash-low"),
            ("openai", "gpt-sol-latest", GPT, "gpt-6.1-sol"),
            ("openai", "gpt-luna-latest", GPT, "gpt-5.6-luna"),
        ],
    )
    def test_alias_names_the_highest_version_of_its_family(
        self, vendor, alias, catalogue, expected
    ):
        assert resolve(vendor, alias, catalogue) == expected

    def test_version_decides_not_catalogue_order(self):
        assert resolve("anthropic", "claude-opus-latest", ("claude-opus-6", *CLAUDE)) == (
            "claude-opus-6"
        )
        assert resolve("anthropic", "claude-opus-latest", (*CLAUDE, "claude-opus-6")) == (
            "claude-opus-6"
        )

    def test_family_less_alias_takes_the_first_of_the_newest_version(self):
        assert resolve("openai", "gpt-latest", GPT) == "gpt-6.1-sol"

    def test_a_minor_release_beats_the_bare_major(self):
        assert resolve("openai", "gpt-sol-latest", ("gpt-6-sol", "gpt-6.1-sol")) == "gpt-6.1-sol"

    @pytest.mark.parametrize(
        ("alias", "expected"),
        [
            ("opus-latest", "claude-opus-5-5"),
            ("claude-latest", "claude-opus-5-5"),
            ("claude-opus-last", "claude-opus-5-5"),
            ("  Claude-Sonnet-Latest ", "claude-sonnet-5-5"),
        ],
    )
    def test_short_and_loosely_spelled_aliases(self, alias, expected):
        assert resolve("anthropic", alias, CLAUDE) == expected

    def test_retired_gemini_pro_alias_follows_argon(self):
        assert resolve("google", "gemini-pro-latest", GEMINI) == "gemini-4-argon-high"

    def test_partner_models_served_by_antigravity_are_never_candidates(self):
        assert resolve("google", "gemini-flash-latest", GEMINI) == "gemini-3.8-flash-high"

    @pytest.mark.parametrize(
        ("vendor", "model", "catalogue"),
        [
            ("anthropic", "claude-opus-5-5", CLAUDE),  # concrete id
            ("anthropic", "my-azure-deployment", CLAUDE),  # free-form string
            ("anthropic", "gpt-latest", CLAUDE),  # another vendor's alias
            ("glm", "glm-latest", ("glm-5.3",)),  # vendor without aliases
            ("anthropic", "claude-opus-latest", ()),  # no candidate
            ("anthropic", "", CLAUDE),
        ],
    )
    def test_anything_else_is_returned_unchanged(self, vendor, model, catalogue):
        assert resolve(vendor, model, catalogue) == model


class TestLatestAliases:
    def test_lists_each_alias_with_its_current_target(self):
        aliases = {a.id: a.resolves_to for a in model_aliases.latest_aliases("openai", GPT)}

        assert aliases["gpt-latest"] == "gpt-6.1-sol"
        assert aliases["gpt-astra-latest"] == "gpt-6-astra"

    def test_omits_an_alias_with_no_candidate(self):
        ids = {a.id for a in model_aliases.latest_aliases("anthropic", ("claude-opus-5-5",))}

        assert ids == {"claude-opus-latest"}

    def test_gemini_aliases_cover_every_effort_tier(self):
        ids = [spec.id for spec in model_aliases.ALIAS_SPECS["google"]]

        assert ids == [
            "gemini-argon-latest",
            "gemini-argon-medium-latest",
            "gemini-argon-low-latest",
            "gemini-flash-latest",
            "gemini-flash-medium-latest",
            "gemini-flash-low-latest",
        ]
