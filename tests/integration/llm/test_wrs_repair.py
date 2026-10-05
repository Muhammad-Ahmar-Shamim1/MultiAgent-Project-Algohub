"""The router's JSON-repair path (Step 05) actually works against WebRequirementsSpec.

Unlike tests/integration/orchestrator/test_requirement_analyzer.py, this mocks litellm itself and
calls the real `complete()`, proving the schema is repair-compatible end to end - not just that
RequirementAnalyzer handles an already-valid mocked result.
"""

from types import SimpleNamespace
from typing import Any

import litellm
import pytest

from web_department.llm.router import (
    AllProvidersFailedError,
    ProviderConfig,
    ProviderTag,
    Purpose,
    complete,
)
from web_department.orchestrator.wrs_schema import WebRequirementsSpec

LOCAL = ProviderConfig(
    name="local",
    model="ollama_chat/qwen3",
    tag=ProviderTag.PRIVATE,
    context_window=8192,
    strengths=[Purpose.PLANNING],
    api_base_env="ollama_base_url",
)

VALID_WRS_JSON = """{
  "project_type": "web_application",
  "domain": "todo",
  "roles": ["user"],
  "features": [
    {"id": "F-01", "name": "authentication", "description": "Login",
     "acceptance": ["User can log in"]}
  ]
}"""

MISSING_ACCEPTANCE_JSON = """{
  "project_type": "web_application",
  "domain": "todo",
  "roles": ["user"],
  "features": [
    {"id": "F-01", "name": "authentication", "description": "Login", "acceptance": []}
  ]
}"""


def _response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


async def test_valid_wrs_json_is_accepted_on_the_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return _response(VALID_WRS_JSON)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await complete(
        [{"role": "user", "content": "produce a wrs"}],
        Purpose.PLANNING,
        json_schema=WebRequirementsSpec,
        providers=[LOCAL],
    )

    assert calls == 1
    assert isinstance(result.parsed, WebRequirementsSpec)
    assert result.parsed.domain == "todo"


async def test_wrs_missing_acceptance_is_rejected_and_repaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_acompletion(*, messages: list[dict[str, str]], **_: Any) -> SimpleNamespace:
        calls.append(messages[-1]["content"])
        if len(calls) == 1:
            return _response(MISSING_ACCEPTANCE_JSON)
        assert "acceptance" in calls[-1]  # the repair prompt names the validation error
        return _response(VALID_WRS_JSON)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await complete(
        [{"role": "user", "content": "produce a wrs"}],
        Purpose.PLANNING,
        json_schema=WebRequirementsSpec,
        providers=[LOCAL],
    )

    assert len(calls) == 2
    assert isinstance(result.parsed, WebRequirementsSpec)
    assert result.parsed.features[0].acceptance == ["User can log in"]


async def test_wrs_still_invalid_after_repair_fails_the_provider_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_acompletion(**_: Any) -> SimpleNamespace:
        return _response(MISSING_ACCEPTANCE_JSON)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    with pytest.raises(AllProvidersFailedError, match="invalid JSON after one repair retry"):
        await complete(
            [{"role": "user", "content": "produce a wrs"}],
            Purpose.PLANNING,
            json_schema=WebRequirementsSpec,
            providers=[LOCAL],
        )
