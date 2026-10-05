"""Model Router: fallback order, confidential routing, caching, budgets, JSON repair, logging.

LiteLLM itself is mocked throughout (no network calls) - these tests exercise the router's own
logic against real Postgres (LLMCall audit rows) and real Redis (cache + daily usage budget).
"""

from types import SimpleNamespace
from typing import Any

import litellm
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from web_department.config.settings import get_settings
from web_department.db.models import LLMCall
from web_department.db.repositories import AgentRepository, BlueprintRepository, TaskRepository
from web_department.llm import router
from web_department.llm.router import (
    AllProvidersFailedError,
    ProviderConfig,
    ProviderTag,
    Purpose,
    complete,
    embed,
)


def _response(content: str, tokens_in: int = 10, tokens_out: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out),
    )


def _rate_limit_error(provider: str) -> litellm.exceptions.RateLimitError:
    return litellm.exceptions.RateLimitError(
        message="rate limited", llm_provider=provider, model="m"
    )


def _timeout_error(provider: str) -> litellm.exceptions.Timeout:
    return litellm.exceptions.Timeout(message="timed out", llm_provider=provider, model="m")


LOCAL = ProviderConfig(
    name="local",
    model="ollama_chat/qwen3",
    tag=ProviderTag.PRIVATE,
    context_window=8192,
    strengths=[Purpose.PLANNING, Purpose.CODING, Purpose.REVIEW],
    api_base_env="ollama_base_url",
)
SHARED_1 = ProviderConfig(
    name="shared1",
    model="groq/llama",
    tag=ProviderTag.SHARED,
    rpd=2,
    context_window=8192,
    strengths=[Purpose.PLANNING, Purpose.CODING, Purpose.REVIEW],
    api_key_env="groq_api_key",
)
SHARED_2 = ProviderConfig(
    name="shared2",
    model="cerebras/llama",
    tag=ProviderTag.SHARED,
    context_window=8192,
    strengths=[Purpose.PLANNING, Purpose.CODING, Purpose.REVIEW],
    api_key_env="cerebras_api_key",
)
EMBEDDER = ProviderConfig(
    name="embedder",
    model="ollama/nomic-embed-text",
    tag=ProviderTag.PRIVATE,
    context_window=2048,
    strengths=[Purpose.EMBEDDING],
    api_base_env="ollama_base_url",
)


@pytest.fixture(autouse=True)
def _configured_shared_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk_test")
    get_settings.cache_clear()


async def _llm_call_rows(session_factory: Any) -> list[LLMCall]:
    async with session_factory() as check:
        result = await check.scalars(select(LLMCall).order_by(LLMCall.created_at))
        return list(result.all())


# --- fallback order -----------------------------------------------------------------------


async def test_first_provider_success_needs_no_fallback(
    monkeypatch: pytest.MonkeyPatch, session_factory: Any
) -> None:
    calls: list[str] = []

    async def fake_acompletion(*, model: str, **_: Any) -> SimpleNamespace:
        calls.append(model)
        return _response("hi from local")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await complete(
        [{"role": "user", "content": "hi"}],
        Purpose.CODING,
        providers=[LOCAL, SHARED_1],
    )

    assert calls == [LOCAL.model]
    assert result.provider == "local"
    assert result.content == "hi from local"
    assert result.cached is False
    assert result.tokens_in == 10
    assert result.tokens_out == 5
    (row,) = await _llm_call_rows(session_factory)
    assert row.provider == "local" and row.status == "ok" and row.cached is False


async def test_falls_back_to_next_provider_on_rate_limit(
    monkeypatch: pytest.MonkeyPatch, session_factory: Any
) -> None:
    calls: list[str] = []

    async def fake_acompletion(*, model: str, **_: Any) -> SimpleNamespace:
        calls.append(model)
        if model == LOCAL.model:
            raise _rate_limit_error("local")
        return _response("hi from shared1")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await complete(
        [{"role": "user", "content": "hi"}],
        Purpose.CODING,
        providers=[LOCAL, SHARED_1],
    )

    assert calls == [LOCAL.model, SHARED_1.model]
    assert result.provider == "shared1"
    rows = await _llm_call_rows(session_factory)
    assert [(r.provider, r.status) for r in rows] == [("local", "error"), ("shared1", "ok")]


async def test_falls_back_on_timeout_too(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(*, model: str, **_: Any) -> SimpleNamespace:
        if model == LOCAL.model:
            raise _timeout_error("local")
        return _response("ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await complete(
        [{"role": "user", "content": "hi"}], Purpose.CODING, providers=[LOCAL, SHARED_1]
    )

    assert result.provider == "shared1"


async def test_stops_after_three_provider_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    attempted: list[str] = []

    async def fake_acompletion(*, model: str, **_: Any) -> SimpleNamespace:
        attempted.append(model)
        raise _rate_limit_error("x")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setenv("OPENROUTER_API_KEY", "ork_test")
    monkeypatch.setenv("GITHUB_MODELS_TOKEN", "ghm_test")
    get_settings.cache_clear()

    def shared(name: str, model: str, key_env: str) -> ProviderConfig:
        return ProviderConfig(
            name=name,
            model=model,
            tag=ProviderTag.SHARED,
            context_window=1,
            strengths=[Purpose.REVIEW],
            api_key_env=key_env,
        )

    providers = [
        shared("s1", "m1", "groq_api_key"),
        shared("s2", "m2", "cerebras_api_key"),
        shared("s3", "m3", "openrouter_api_key"),
        shared("s4", "m4", "github_models_token"),
    ]

    with pytest.raises(AllProvidersFailedError, match="s1.*s2.*s3"):
        await complete([{"role": "user", "content": "hi"}], Purpose.REVIEW, providers=providers)

    assert attempted == ["m1", "m2", "m3"]


async def test_all_providers_failing_raises_with_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        raise _rate_limit_error("x")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    with pytest.raises(AllProvidersFailedError, match="local.*rate limited"):
        await complete([{"role": "user", "content": "hi"}], Purpose.CODING, providers=[LOCAL])


async def test_no_configured_provider_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    get_settings.cache_clear()

    with pytest.raises(AllProvidersFailedError, match="no provider is configured"):
        await complete([{"role": "user", "content": "hi"}], Purpose.CODING, providers=[SHARED_1])


# --- confidential routing ------------------------------------------------------------------


async def test_confidential_never_calls_a_shared_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    called_models: list[str] = []

    async def fake_acompletion(*, model: str, **_: Any) -> SimpleNamespace:
        called_models.append(model)
        return _response("private reply")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await complete(
        [{"role": "user", "content": "secret"}],
        Purpose.PLANNING,
        confidential=True,
        providers=[LOCAL, SHARED_1],
    )

    assert called_models == [LOCAL.model]
    assert result.provider == "local"


async def test_confidential_with_only_shared_providers_raises_without_calling_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        raise AssertionError("must not call a shared provider when confidential=True")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    with pytest.raises(AllProvidersFailedError):
        await complete(
            [{"role": "user", "content": "secret"}],
            Purpose.CODING,
            confidential=True,
            providers=[SHARED_1],
        )


# --- cache ---------------------------------------------------------------------------------


async def test_second_identical_call_is_served_from_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        nonlocal call_count
        call_count += 1
        return _response("cached me")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    messages = [{"role": "user", "content": "same question"}]

    first = await complete(messages, Purpose.CODING, providers=[LOCAL])
    second = await complete(messages, Purpose.CODING, providers=[LOCAL])

    assert call_count == 1
    assert first.cached is False
    assert second.cached is True
    assert second.content == "cached me"
    assert second.tokens_in == 0 and second.tokens_out == 0


async def test_different_messages_are_different_cache_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def fake_acompletion(*, messages: list[dict[str, str]], **_: Any) -> SimpleNamespace:
        seen.append(messages[0]["content"])
        return _response(f"reply to {messages[0]['content']}")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    a = await complete([{"role": "user", "content": "A"}], Purpose.CODING, providers=[LOCAL])
    b = await complete([{"role": "user", "content": "B"}], Purpose.CODING, providers=[LOCAL])

    assert seen == ["A", "B"]
    assert (a.content, b.content) == ("reply to A", "reply to B")


async def test_cache_is_scoped_per_provider_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(*, model: str, **_: Any) -> SimpleNamespace:
        return _response(f"from {model}")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    messages = [{"role": "user", "content": "hi"}]

    from_local = await complete(messages, Purpose.CODING, providers=[LOCAL])
    from_shared = await complete(messages, Purpose.CODING, confidential=False, providers=[SHARED_1])

    assert from_local.cached is False
    assert from_shared.cached is False
    assert from_local.content != from_shared.content


# --- daily budget ----------------------------------------------------------------------------


async def test_provider_past_its_daily_budget_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def fake_acompletion(*, model: str, **_: Any) -> SimpleNamespace:
        called.append(model)
        return _response(f"reply {len(called)}")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    budgeted = ProviderConfig(
        name="budgeted",
        model="groq/budgeted",
        tag=ProviderTag.SHARED,
        rpd=2,
        context_window=8192,
        strengths=[Purpose.CODING],
        api_key_env="groq_api_key",
    )
    messages_for = [[{"role": "user", "content": f"q{i}"}] for i in range(3)]

    r1 = await complete(messages_for[0], Purpose.CODING, providers=[budgeted])
    r2 = await complete(messages_for[1], Purpose.CODING, providers=[budgeted])
    with pytest.raises(AllProvidersFailedError, match="daily budget exhausted"):
        await complete(messages_for[2], Purpose.CODING, providers=[budgeted])

    assert r1.provider == r2.provider == "budgeted"
    assert len(called) == 2


async def test_budget_skip_falls_through_to_next_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    exhausted = ProviderConfig(
        name="exhausted",
        model="groq/exhausted",
        tag=ProviderTag.SHARED,
        rpd=1,
        context_window=8192,
        strengths=[Purpose.CODING],
        api_key_env="groq_api_key",
    )

    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        return _response("ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    await complete([{"role": "user", "content": "warm up"}], Purpose.CODING, providers=[exhausted])

    result = await complete(
        [{"role": "user", "content": "next"}], Purpose.CODING, providers=[exhausted, SHARED_2]
    )

    assert result.provider == "shared2"


async def test_cache_hits_do_not_consume_the_daily_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return _response("same answer")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    limited = ProviderConfig(
        name="limited",
        model="groq/limited",
        tag=ProviderTag.SHARED,
        rpd=2,
        context_window=8192,
        strengths=[Purpose.CODING],
        api_key_env="groq_api_key",
    )
    same_message = [{"role": "user", "content": "repeat me"}]

    for _ in range(3):
        result = await complete(same_message, Purpose.CODING, providers=[limited])
        assert result.provider == "limited"

    assert calls == 1  # only the first call was genuine; the rest were cache hits
    other_message = [{"role": "user", "content": "a different question"}]
    second = await complete(other_message, Purpose.CODING, providers=[limited])
    assert second.provider == "limited"
    assert calls == 2  # the budget still had room for one more genuine call


async def test_unlimited_rpd_provider_is_never_budget_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert LOCAL.rpd is None

    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        return _response("ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    for i in range(5):
        result = await complete(
            [{"role": "user", "content": f"q{i}"}], Purpose.CODING, providers=[LOCAL]
        )
        assert result.provider == "local"


# --- JSON schema + repair ------------------------------------------------------------------


class Plan(BaseModel):
    title: str
    steps: list[str]


async def test_valid_json_is_parsed_on_the_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        return _response('{"title": "Todo app", "steps": ["auth", "crud"]}')

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await complete(
        [{"role": "user", "content": "plan it"}],
        Purpose.PLANNING,
        json_schema=Plan,
        providers=[LOCAL],
    )

    assert isinstance(result.parsed, Plan)
    assert result.parsed.title == "Todo app"
    assert result.parsed.steps == ["auth", "crud"]


async def test_invalid_json_is_repaired_once_and_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def fake_acompletion(*, messages: list[dict[str, str]], **_: Any) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _response("not json at all")
        assert any("not json at all" in m["content"] for m in messages)
        return _response('{"title": "Repaired", "steps": []}')

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await complete(
        [{"role": "user", "content": "plan it"}],
        Purpose.PLANNING,
        json_schema=Plan,
        providers=[LOCAL],
    )

    assert calls == 2
    assert result.parsed == Plan(title="Repaired", steps=[])
    assert result.cached is False


async def test_repair_still_invalid_falls_back_to_next_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acompletion(*, model: str, **_: Any) -> SimpleNamespace:
        if model == LOCAL.model:
            return _response("garbage, still garbage after repair")
        return _response('{"title": "From shared", "steps": ["x"]}')

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await complete(
        [{"role": "user", "content": "plan it"}],
        Purpose.PLANNING,
        json_schema=Plan,
        providers=[LOCAL, SHARED_1],
    )

    assert result.provider == "shared1"
    assert result.parsed == Plan(title="From shared", steps=["x"])


async def test_cached_json_result_reparses_into_the_same_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        return _response('{"title": "Cache me", "steps": ["a"]}')

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    messages = [{"role": "user", "content": "plan"}]

    await complete(messages, Purpose.PLANNING, json_schema=Plan, providers=[LOCAL])
    second = await complete(messages, Purpose.PLANNING, json_schema=Plan, providers=[LOCAL])

    assert second.cached is True
    assert second.parsed == Plan(title="Cache me", steps=["a"])


# --- embed() ---------------------------------------------------------------------------------


async def test_embed_returns_vectors_from_the_embedding_provider(
    monkeypatch: pytest.MonkeyPatch, session_factory: Any
) -> None:
    async def fake_aembedding(*, model: str, input: list[str], **_: Any) -> SimpleNamespace:
        assert model == EMBEDDER.model
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[float(len(t))] * 3) for t in input],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=0),
        )

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)

    vectors = await embed(["hi", "hello there"], providers=[EMBEDDER])

    assert vectors == [[2.0, 2.0, 2.0], [11.0, 11.0, 11.0]]
    (row,) = await _llm_call_rows(session_factory)
    assert row.provider == "embedder" and row.tokens_in == 7


async def test_embed_falls_back_and_raises_when_all_providers_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_aembedding(**_: Any) -> SimpleNamespace:
        raise _rate_limit_error("embedder")

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)

    with pytest.raises(AllProvidersFailedError, match="Embedding failed"):
        await embed(["hi"], providers=[EMBEDDER])


async def test_embed_with_no_embedding_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AllProvidersFailedError):
        await embed(["hi"], providers=[LOCAL])


# --- LLMCall logging (FR-153) ---------------------------------------------------------------


async def test_llm_call_row_records_agent_and_task_ids(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: Any,
    committed_project_id: str,
) -> None:
    # router.py logs via its own connection (session_scope), so the agent/task it references
    # must be genuinely committed - not just saved on the `session` fixture's rolled-back
    # savepoint, which a separate connection can never see. Use session_factory directly, like
    # the committed_project_id fixture does.
    async with session_factory() as own:
        await BlueprintRepository(own).create(id="bp", version="1", yaml="y", checksum="c")
        agent = await AgentRepository(own).create(
            display_id="WEB-XX-001",
            blueprint_id="bp",
            blueprint_version="1",
            project_id=committed_project_id,
        )
        task = await TaskRepository(own).create(
            display_id="WEB-001", project_id=committed_project_id, title="t", role="backend"
        )
        await own.commit()
        agent_id, task_id = agent.id, task.id

    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        return _response("hi")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    await complete(
        [{"role": "user", "content": "hi"}],
        Purpose.CODING,
        agent_id=agent_id,
        task_id=task_id,
        providers=[LOCAL],
    )

    (row,) = await _llm_call_rows(session_factory)
    assert row.agent_id == agent_id
    assert row.task_id == task_id
    assert row.project_id is None


async def test_failed_and_successful_attempts_are_both_logged(
    monkeypatch: pytest.MonkeyPatch, session_factory: Any
) -> None:
    async def fake_acompletion(*, model: str, **_: Any) -> SimpleNamespace:
        if model == LOCAL.model:
            raise _rate_limit_error("local")
        return _response("ok")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    await complete([{"role": "user", "content": "hi"}], Purpose.CODING, providers=[LOCAL, SHARED_1])

    rows = await _llm_call_rows(session_factory)
    assert len(rows) == 2
    assert rows[0].status == "error" and rows[0].latency_ms >= 0
    assert rows[1].status == "ok"


async def test_router_only_uses_openai_for_its_exception_hierarchy() -> None:
    """AGENTS.md rule 6: litellm is the only path to a provider.

    ``openai`` is imported solely because litellm's own exceptions subclass it (verified in
    test_falls_back_to_next_provider_on_rate_limit et al.) - it must never be used to build a
    client or call a provider directly.
    """
    import inspect

    source = inspect.getsource(router)
    assert "import litellm" in source
    assert "import openai" in source
    assert "openai.OpenAI" not in source
    assert "openai.AsyncOpenAI" not in source
    assert "openai.chat" not in source
    assert "openai.completions" not in source
