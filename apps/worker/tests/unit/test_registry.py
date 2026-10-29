"""The model registry is the single source of model IDs."""

from services import agent_backends
from services.agent_backends import VENDOR_MODELS


def test_every_vendor_has_exactly_one_default_listed_first_in_picker():
    for vendor in VENDOR_MODELS:
        defaults = [m for m in VENDOR_MODELS[vendor] if m.is_default]
        assert len(defaults) == 1, vendor
        assert agent_backends.picker_models(vendor)[0].is_default


def test_deprecated_models_are_hidden_from_picker_but_still_known():
    assert "claude-sonnet-4-6" not in {m.id for m in agent_backends.picker_models("anthropic")}
    assert agent_backends.is_known_model("claude-sonnet-4-6")


def test_new_models_are_offered():
    offered = {m.id for vendor in VENDOR_MODELS for m in agent_backends.picker_models(vendor)}
    assert {"claude-haiku-5-5", "gemini-4-argon-high"} <= offered


def test_ids_are_unique_per_vendor():
    for vendor, models in VENDOR_MODELS.items():
        ids = [m.id for m in models]
        assert len(ids) == len(set(ids)), vendor


def test_registry_payload_shape():
    payload = agent_backends.registry_payload()
    assert payload["anthropic"]["default_model"] == agent_backends.default_model("anthropic")
    assert {"id", "label", "is_default", "deprecated"} <= set(payload["glm"]["models"][0])
