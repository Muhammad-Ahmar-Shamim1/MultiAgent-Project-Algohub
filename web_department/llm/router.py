"""The Model Router: the single path every LLM call in this codebase must take (IR-020..IR-024).

AGENTS.md rule 6: no other module may import a provider SDK directly - always call
``complete()`` or ``embed()`` here.

Usage as a CLI (SRS 11.1 manual check)::

    python -m web_department.llm.router --ping

Sends a one-line chat message through the router and prints which provider answered.
"""

import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import litellm
import openai  # only for its exception hierarchy: litellm's own errors subclass it
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from redis.asyncio import Redis

from web_department.config.settings import Settings, get_settings
from web_department.db.base import session_scope
from web_department.db.repositories import LLMCallRepository
from web_department.llm.prompts import render as render_prompt
from web_department.logging import configure_logging, get_logger

log = get_logger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

DEFAULT_PROVIDERS_PATH = Path(__file__).parent / "providers.yaml"
USAGE_KEY_TTL_SECONDS = 2 * 24 * 3600  # outlives a day regardless of when in the day it was set


class ProviderTag(StrEnum):
    """SRS 11.1 / IR-024: `private` stays on this machine; `shared` free tiers may train on it."""

    PRIVATE = "private"
    SHARED = "shared"


class Purpose(StrEnum):
    """A call's purpose, used to pick providers per the SRS 11.1 routing table."""

    PLANNING = "planning"
    CODING = "coding"
    REVIEW = "review"
    EMBEDDING = "embedding"


class ProviderConfig(BaseModel):
    """One entry from providers.yaml."""

    model_config = ConfigDict(frozen=True)

    name: str
    model: str
    tag: ProviderTag
    rpm: int | None = None
    rpd: int | None = None
    context_window: int
    strengths: list[Purpose]
    api_base_env: str | None = None
    api_key_env: str | None = None


@dataclass(frozen=True)
class CompletionResult:
    """The outcome of one successful ``complete()`` call."""

    content: str
    parsed: BaseModel | None
    provider: str
    model: str
    cached: bool
    tokens_in: int
    tokens_out: int
    latency_ms: int


class AllProvidersFailedError(Exception):
    """Raised when every eligible provider for a call failed, or none was configured/available."""


class ProviderCallFailed(Exception):
    """Internal: one provider's attempt (including its repair retry) failed; try the next."""


# --- providers.yaml -------------------------------------------------------------------------


def load_providers(path: Path = DEFAULT_PROVIDERS_PATH) -> list[ProviderConfig]:
    """Parse providers.yaml into ``ProviderConfig`` objects, in file order."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [ProviderConfig.model_validate(entry) for entry in data.get("providers") or []]


_PURPOSE_ORDER: dict[Purpose, tuple[ProviderTag, ...]] = {
    Purpose.PLANNING: (ProviderTag.SHARED, ProviderTag.PRIVATE),
    Purpose.CODING: (ProviderTag.PRIVATE, ProviderTag.SHARED),
    Purpose.REVIEW: (ProviderTag.SHARED, ProviderTag.PRIVATE),
    Purpose.EMBEDDING: (ProviderTag.PRIVATE, ProviderTag.SHARED),
}


def select_providers(
    providers: list[ProviderConfig], purpose: Purpose, *, confidential: bool
) -> list[ProviderConfig]:
    """Order ``providers`` for ``purpose`` per SRS 11.1; drop ``shared`` ones when confidential.

    Planning/architecture and review prefer the strongest free API model, falling back to local;
    coding prefers local, falling back to a free API model; embeddings always prefer local.
    ``confidential=True`` (IR-024) removes every ``shared`` provider regardless of purpose.
    """
    tag_order = _PURPOSE_ORDER[purpose]
    if confidential:
        tag_order = tuple(t for t in tag_order if t is ProviderTag.PRIVATE)
    ordered: list[ProviderConfig] = []
    for tag in tag_order:
        ordered += [p for p in providers if p.tag is tag and purpose in p.strengths]
    return ordered


def is_configured(provider: ProviderConfig, settings: Settings) -> bool:
    """True unless ``provider`` needs an API key that ``settings`` doesn't have."""
    if provider.api_key_env is None:
        return True
    return getattr(settings, provider.api_key_env, None) is not None


def _provider_kwargs(provider: ProviderConfig, settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if provider.api_base_env:
        kwargs["api_base"] = getattr(settings, provider.api_base_env)
    if provider.api_key_env:
        secret = getattr(settings, provider.api_key_env)
        if secret is not None:
            kwargs["api_key"] = secret.get_secret_value()
    return kwargs


# --- Redis: cache + daily usage budget -------------------------------------------------------

_default_redis: Redis | None = None


def get_redis() -> Redis:
    """Return the process-wide Redis client used for the response cache and usage counters."""
    global _default_redis
    if _default_redis is None:
        _default_redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _default_redis


def set_redis(client: Redis | None) -> None:
    """Replace (or reset with None) the process-wide Redis client, e.g. in tests."""
    global _default_redis
    _default_redis = client


async def close_redis() -> None:
    """Close and forget the process-wide Redis client, if one was created."""
    global _default_redis
    if _default_redis is not None:
        await _default_redis.aclose()
        _default_redis = None


def _cache_key(
    model: str, messages: list[dict[str, str]], json_schema: type[BaseModel] | None
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "schema": json_schema.model_json_schema() if json_schema is not None else None,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"llm:cache:{digest}"


def _usage_key(provider_name: str) -> str:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"llm:usage:{provider_name}:{day}"


async def _within_budget(redis: Redis, provider: ProviderConfig) -> bool:
    """True if ``provider`` has not yet made its ``rpd`` calls today (``rpd=None`` = unlimited)."""
    if provider.rpd is None:
        return True
    used = await redis.get(_usage_key(provider.name))
    return int(used or 0) < provider.rpd


async def has_remaining_budget(
    purpose: Purpose | str,
    *,
    confidential: bool = False,
    providers: list[ProviderConfig] | None = None,
) -> bool:
    """True if at least one eligible, configured provider for ``purpose`` has daily budget left.

    Mirrors ``complete()``'s own candidate selection exactly, so a caller (the scheduler, FR-035)
    can check *before* starting a task whether it would even get to make a call, and back off
    instead of burning one of that task's own retries on a call that would only fail for lack of
    budget - a provider with an active `ProviderCallFailed`-style outage isn't checked here, only
    the ``rpd`` daily cap ``complete()`` itself enforces.
    """
    purpose = Purpose(purpose)
    settings = get_settings()
    redis = get_redis()
    pool = providers if providers is not None else load_providers()
    candidates = [
        p
        for p in select_providers(pool, purpose, confidential=confidential)
        if is_configured(p, settings)
    ]
    for provider in candidates:
        if await _within_budget(redis, provider):
            return True
    return False


async def _record_usage(redis: Redis, provider: ProviderConfig) -> None:
    key = _usage_key(provider.name)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, USAGE_KEY_TTL_SECONDS)
        await pipe.execute()


# --- LiteLLM calls ----------------------------------------------------------------------------


def _extract_content(response: Any) -> str:
    return str(response.choices[0].message.content or "")


def _extract_tokens(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return 0, 0
    tokens_in = getattr(usage, "prompt_tokens", None)
    if tokens_in is None and isinstance(usage, dict):
        tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = getattr(usage, "completion_tokens", None)
    if tokens_out is None and isinstance(usage, dict):
        tokens_out = usage.get("completion_tokens", 0)
    return int(tokens_in or 0), int(tokens_out or 0)


async def _acompletion(
    provider: ProviderConfig, messages: list[dict[str, str]], *, json_mode: bool, settings: Settings
) -> Any:
    kwargs = _provider_kwargs(provider, settings)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        return await litellm.acompletion(
            model=provider.model, messages=messages, timeout=60, **kwargs
        )
    except openai.APIError as exc:
        raise ProviderCallFailed(str(exc)) from exc


def _try_validate(content: str, schema: type[SchemaT]) -> tuple[SchemaT | None, str | None]:
    try:
        return schema.model_validate_json(content), None
    except (ValidationError, ValueError) as exc:
        return None, str(exc)


async def _validate_with_repair(
    provider: ProviderConfig,
    messages: list[dict[str, str]],
    content: str,
    json_schema: type[SchemaT],
    settings: Settings,
) -> tuple[SchemaT, str, int, int]:
    """Validate ``content`` against ``json_schema``; one repair round-trip if it fails."""
    parsed, error = _try_validate(content, json_schema)
    if parsed is not None:
        return parsed, content, 0, 0

    repair_prompt = render_prompt(
        "json_repair",
        schema=json.dumps(json_schema.model_json_schema()),
        error=error,
        previous_response=content,
    )
    repair_messages = [
        *messages,
        {"role": "assistant", "content": content},
        {"role": "user", "content": repair_prompt},
    ]
    response = await _acompletion(provider, repair_messages, json_mode=True, settings=settings)
    repaired_content = _extract_content(response)
    tokens_in, tokens_out = _extract_tokens(response)
    parsed, error = _try_validate(repaired_content, json_schema)
    if parsed is None:
        raise ProviderCallFailed(f"invalid JSON after one repair retry: {error}")
    return parsed, repaired_content, tokens_in, tokens_out


async def _call_provider(
    redis: Redis,
    provider: ProviderConfig,
    messages: list[dict[str, str]],
    json_schema: type[SchemaT] | None,
    settings: Settings,
) -> CompletionResult:
    """Serve ``provider`` from cache if possible, else call it (with JSON repair if needed)."""
    cache_key = _cache_key(provider.model, messages, json_schema)
    cached_raw = await redis.get(cache_key)
    if cached_raw is not None:
        cached = json.loads(cached_raw)
        cached_parsed = (
            json_schema.model_validate(cached["parsed"])
            if json_schema is not None and cached["parsed"] is not None
            else None
        )
        return CompletionResult(
            content=cached["content"],
            parsed=cached_parsed,
            provider=provider.name,
            model=provider.model,
            cached=True,
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
        )

    start = time.monotonic()
    response = await _acompletion(
        provider, messages, json_mode=json_schema is not None, settings=settings
    )
    content = _extract_content(response)
    tokens_in, tokens_out = _extract_tokens(response)

    parsed: SchemaT | None = None
    if json_schema is not None:
        parsed, content, more_in, more_out = await _validate_with_repair(
            provider, messages, content, json_schema, settings
        )
        tokens_in += more_in
        tokens_out += more_out
    latency_ms = int((time.monotonic() - start) * 1000)

    await redis.set(
        cache_key,
        json.dumps(
            {"content": content, "parsed": parsed.model_dump() if parsed is not None else None}
        ),
        ex=settings.llm_cache_ttl_days * 86400,
    )
    return CompletionResult(
        content=content,
        parsed=parsed,
        provider=provider.name,
        model=provider.model,
        cached=False,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
    )


async def _log_llm_call(
    *,
    provider: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    cached: bool,
    status: str,
    agent_id: str | None,
    task_id: str | None,
) -> None:
    """Persist one LLMCall row (FR-153); its own transaction so logging never blocks the caller."""
    async with session_scope() as session:
        await LLMCallRepository(session).create(
            agent_id=agent_id,
            task_id=task_id,
            project_id=None,
            provider=provider,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            cached=cached,
            status=status,
        )


# --- Public API ---------------------------------------------------------------------------


async def complete(
    messages: list[dict[str, str]],
    purpose: Purpose | str,
    *,
    confidential: bool = False,
    json_schema: type[SchemaT] | None = None,
    agent_id: str | None = None,
    task_id: str | None = None,
    providers: list[ProviderConfig] | None = None,
) -> CompletionResult:
    """Complete a chat conversation through the Model Router.

    Tries eligible providers in SRS 11.1 order, up to ``Settings.llm_max_attempts`` total (a
    budget-exhausted provider is skipped without counting against that limit). ``shared``
    providers are skipped entirely when ``confidential=True`` (IR-024). When ``json_schema`` is
    given the response is parsed as JSON and validated against it, with one repair round-trip per
    provider on invalid JSON. Successful responses are cached in Redis by
    sha256(model + messages + schema); every attempt (success or failure) is logged as an
    ``LLMCall`` row. Raises ``AllProvidersFailedError`` if no provider could serve the request.
    """
    purpose = Purpose(purpose)
    settings = get_settings()
    redis = get_redis()
    pool = providers if providers is not None else load_providers()
    candidates = [
        p
        for p in select_providers(pool, purpose, confidential=confidential)
        if is_configured(p, settings)
    ]

    errors: list[str] = []
    for provider in candidates[: settings.llm_max_attempts]:
        if not await _within_budget(redis, provider):
            errors.append(f"{provider.name}: daily budget exhausted")
            continue
        attempt_start = time.monotonic()
        try:
            result = await _call_provider(redis, provider, messages, json_schema, settings)
        except ProviderCallFailed as exc:
            latency_ms = int((time.monotonic() - attempt_start) * 1000)
            errors.append(f"{provider.name}: {exc}")
            await _log_llm_call(
                provider=provider.name,
                model=provider.model,
                tokens_in=0,
                tokens_out=0,
                latency_ms=latency_ms,
                cached=False,
                status="error",
                agent_id=agent_id,
                task_id=task_id,
            )
            continue
        if not result.cached:
            # A cache hit made no real API call, so it must not count against the daily budget.
            await _record_usage(redis, provider)
        await _log_llm_call(
            provider=result.provider,
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            latency_ms=result.latency_ms,
            cached=result.cached,
            status="ok",
            agent_id=agent_id,
            task_id=task_id,
        )
        return result

    reason = "; ".join(errors) if errors else "no provider is configured for this purpose"
    raise AllProvidersFailedError(
        f"No provider served purpose={purpose.value} confidential={confidential}: {reason}"
    )


async def embed(
    texts: list[str], *, providers: list[ProviderConfig] | None = None
) -> list[list[float]]:
    """Embed ``texts`` via the configured embedding provider(s) (SRS 11.1: local, always)."""
    settings = get_settings()
    redis = get_redis()
    pool = providers if providers is not None else load_providers()
    candidates = [
        p
        for p in select_providers(pool, Purpose.EMBEDDING, confidential=False)
        if is_configured(p, settings)
    ]

    errors: list[str] = []
    for provider in candidates[: settings.llm_max_attempts]:
        if not await _within_budget(redis, provider):
            errors.append(f"{provider.name}: daily budget exhausted")
            continue
        start = time.monotonic()
        try:
            kwargs = _provider_kwargs(provider, settings)
            response = await litellm.aembedding(model=provider.model, input=texts, **kwargs)
        except openai.APIError as exc:
            errors.append(f"{provider.name}: {exc}")
            continue
        latency_ms = int((time.monotonic() - start) * 1000)
        vectors = [
            item["embedding"] if isinstance(item, dict) else item.embedding
            for item in response.data
        ]
        tokens_in, _ = _extract_tokens(response)
        await _record_usage(redis, provider)
        await _log_llm_call(
            provider=provider.name,
            model=provider.model,
            tokens_in=tokens_in,
            tokens_out=0,
            latency_ms=latency_ms,
            cached=False,
            status="ok",
            agent_id=None,
            task_id=None,
        )
        return list(vectors)

    reason = "; ".join(errors) if errors else "no embedding provider is configured"
    raise AllProvidersFailedError(f"Embedding failed: {reason}")


# --- CLI: python -m web_department.llm.router --ping -----------------------------------------


async def _ping() -> None:
    configure_logging(get_settings().log_level)
    try:
        result = await complete(
            [{"role": "user", "content": "Reply with exactly one word: pong"}],
            purpose=Purpose.CODING,
        )
    except AllProvidersFailedError as exc:
        print(f"FAILED: {exc}")
        raise SystemExit(1) from None
    print(f"[{result.provider} / {result.model}] {result.content.strip()}")
    await close_redis()


def main() -> None:
    if "--ping" not in sys.argv:
        raise SystemExit(__doc__)
    asyncio.run(_ping())


if __name__ == "__main__":
    main()
