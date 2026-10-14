from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.providers import search_model


def test_anthropic_without_a_key_no_longer_stops_the_app(monkeypatch):
    # Keys live in model connections now: a missing ANTHROPIC_API_KEY must not crash the API or the worker.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert Settings(llm_provider="anthropic").anthropic_api_key is None


def test_anthropic_max_tokens_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "8192")

    assert Settings().anthropic_max_tokens == 8192
    assert Settings.model_fields["anthropic_max_tokens"].default == 64_000


def test_discovery_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("PAPERLAB_STACK", "paperlab-e2e")  # fake providers only run on a test stack (D273)
    monkeypatch.setenv("DISCOVERY_PROVIDER", "fake")
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "secret-key")

    assert (Settings().discovery_provider, Settings().semantic_scholar_api_key) == ("fake", "secret-key")
    assert Settings.model_fields["discovery_provider"].default == "live"
    assert Settings.model_fields["semantic_scholar_api_key"].default == ""


def test_the_embedding_model_name_follows_the_shipped_search_model():
    # D137: chunks record the variant's name; the model itself lives in the models volume.
    assert Settings.model_fields["embed_model"].default == search_model.SHIPPED.name
    assert Settings.model_fields["models_dir"].default == Path("/models")


@pytest.mark.parametrize(
    "llm, discovery, stack",
    [
        ("fake", "live", "paperlab"),
        ("ollama", "fake", "paperlab"),
        ("fake", "fake", ""),
        ("fake", "live", "paperlab-app"),
    ],
)
def test_fake_needs_a_test_stack(monkeypatch, llm, discovery, stack):
    monkeypatch.setenv("LLM_PROVIDER", llm)
    monkeypatch.setenv("DISCOVERY_PROVIDER", discovery)
    monkeypatch.setenv("PAPERLAB_STACK", stack)
    with pytest.raises(ValidationError, match="scripts/e2e-stack.sh"):
        Settings()


@pytest.mark.parametrize("stack", ["paperlab-e2e", "paperlab-release-e2e"])
def test_a_test_stack_may_run_fake_providers(monkeypatch, stack):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("DISCOVERY_PROVIDER", "fake")
    monkeypatch.setenv("PAPERLAB_STACK", stack)
    assert (Settings().llm_provider, Settings().paperlab_stack) == ("fake", stack)


def test_real_providers_run_on_any_stack(monkeypatch):
    monkeypatch.setenv("PAPERLAB_STACK", "paperlab")
    assert Settings().llm_provider == "ollama"
