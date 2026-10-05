"""Provider loading and SRS 11.1 ordering/confidential/configuration filtering."""

from pathlib import Path

from web_department.config.settings import Settings
from web_department.llm.router import (
    DEFAULT_PROVIDERS_PATH,
    ProviderConfig,
    ProviderTag,
    Purpose,
    is_configured,
    load_providers,
    select_providers,
)

LOCAL = ProviderConfig(
    name="local",
    model="ollama_chat/x",
    tag=ProviderTag.PRIVATE,
    context_window=8192,
    strengths=[Purpose.PLANNING, Purpose.CODING, Purpose.REVIEW],
    api_base_env="ollama_base_url",
)
SHARED_1 = ProviderConfig(
    name="shared1",
    model="groq/x",
    tag=ProviderTag.SHARED,
    context_window=8192,
    strengths=[Purpose.PLANNING, Purpose.CODING, Purpose.REVIEW],
    api_key_env="groq_api_key",
)
SHARED_2 = ProviderConfig(
    name="shared2",
    model="cerebras/x",
    tag=ProviderTag.SHARED,
    context_window=8192,
    strengths=[Purpose.PLANNING, Purpose.REVIEW],
    api_key_env="cerebras_api_key",
)
EMBED = ProviderConfig(
    name="embed",
    model="ollama/nomic",
    tag=ProviderTag.PRIVATE,
    context_window=2048,
    strengths=[Purpose.EMBEDDING],
    api_base_env="ollama_base_url",
)
ALL = [LOCAL, SHARED_1, SHARED_2, EMBED]


def test_coding_prefers_local_then_shared_in_declared_order() -> None:
    assert select_providers(ALL, Purpose.CODING, confidential=False) == [LOCAL, SHARED_1]


def test_planning_prefers_shared_then_local() -> None:
    assert select_providers(ALL, Purpose.PLANNING, confidential=False) == [
        SHARED_1,
        SHARED_2,
        LOCAL,
    ]


def test_review_prefers_shared_then_local() -> None:
    assert select_providers(ALL, Purpose.REVIEW, confidential=False) == [SHARED_1, SHARED_2, LOCAL]


def test_embedding_only_matches_embedding_strength() -> None:
    assert select_providers(ALL, Purpose.EMBEDDING, confidential=False) == [EMBED]


def test_confidential_drops_every_shared_provider_regardless_of_purpose() -> None:
    assert select_providers(ALL, Purpose.CODING, confidential=True) == [LOCAL]
    assert select_providers(ALL, Purpose.PLANNING, confidential=True) == [LOCAL]
    assert select_providers(ALL, Purpose.REVIEW, confidential=True) == [LOCAL]


def test_confidential_can_leave_no_candidates() -> None:
    assert select_providers([SHARED_1], Purpose.CODING, confidential=True) == []


def test_provider_not_matching_purpose_strength_is_excluded() -> None:
    assert SHARED_2 not in select_providers(ALL, Purpose.CODING, confidential=False)


def test_is_configured_for_private_shared_and_missing_key() -> None:
    settings = Settings(_env_file=None, groq_api_key="k")

    assert is_configured(LOCAL, settings) is True
    assert is_configured(SHARED_1, settings) is True
    assert is_configured(SHARED_2, settings) is False


def test_real_providers_yaml_has_the_two_ollama_entries_plus_groq() -> None:
    """Groq is enabled as of STEP 13's live R1 validation run: no private/local provider was
    available, and the user explicitly opted into a shared/free-tier fallback for that run (see
    Settings.coder_confidential, which independently still defaults to keeping the Coder's own
    code off shared providers regardless of what's enabled here)."""
    providers = load_providers(DEFAULT_PROVIDERS_PATH)

    assert {p.name for p in providers} == {
        "ollama-qwen3-8b",
        "ollama-nomic-embed-text",
        "groq-llama-3.3-70b",
    }
    coding = next(p for p in providers if p.name == "ollama-qwen3-8b")
    assert coding.tag is ProviderTag.PRIVATE
    assert coding.rpd is None
    assert set(coding.strengths) == {Purpose.PLANNING, Purpose.CODING, Purpose.REVIEW}
    assert coding.model.startswith("ollama_chat/")
    embedder = next(p for p in providers if p.name == "ollama-nomic-embed-text")
    assert embedder.strengths == [Purpose.EMBEDDING]
    groq = next(p for p in providers if p.name == "groq-llama-3.3-70b")
    assert groq.tag is ProviderTag.SHARED
    assert groq.api_key_env == "groq_api_key"


def test_load_providers_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("providers: []\n")

    assert load_providers(path) == []
