"""Tests for web_department.config.settings."""

import pytest
from pydantic import ValidationError

from web_department.config.environments import Environment
from web_department.config.settings import Settings, get_settings

ENV_VARS = [
    "ENV",
    "DEBUG",
    "LOG_LEVEL",
    "DATABASE_URL",
    "REDIS_URL",
    "OLLAMA_BASE_URL",
    "MAX_TASK_RETRIES",
    "MAX_PARALLEL_TASKS",
    "WORKSTATION_CPU",
    "WORKSTATION_MEM",
    "TASK_TIMEOUT_MIN",
    "LLM_MAX_ATTEMPTS",
    "LLM_CACHE_TTL_DAYS",
    "GROQ_API_KEY",
    "CEREBRAS_API_KEY",
    "OPENROUTER_API_KEY",
    "GITHUB_MODELS_TOKEN",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def test_defaults_when_no_env_vars_set() -> None:
    settings = Settings(_env_file=None)

    assert settings.env is Environment.DEV
    assert settings.debug is False
    assert settings.log_level == "INFO"
    assert settings.max_task_retries == 3
    assert settings.max_parallel_tasks == 2
    assert settings.workstation_cpu == 2
    assert settings.workstation_mem == "4g"
    assert settings.task_timeout_min == 30
    assert settings.llm_max_attempts == 3
    assert settings.llm_cache_ttl_days == 7
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.groq_api_key is None


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "staging")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("REDIS_URL", "redis://cache:6379/1")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setenv("MAX_TASK_RETRIES", "5")
    monkeypatch.setenv("MAX_PARALLEL_TASKS", "4")
    monkeypatch.setenv("WORKSTATION_CPU", "1.5")
    monkeypatch.setenv("WORKSTATION_MEM", "512m")
    monkeypatch.setenv("TASK_TIMEOUT_MIN", "45")
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("LLM_CACHE_TTL_DAYS", "14")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test123")

    settings = Settings(_env_file=None)

    assert settings.env is Environment.STAGING
    assert settings.debug is True
    assert settings.log_level == "DEBUG"
    assert settings.database_url == "postgresql+asyncpg://u:p@db:5432/x"
    assert settings.redis_url == "redis://cache:6379/1"
    assert settings.ollama_base_url == "http://ollama:11434"
    assert settings.max_task_retries == 5
    assert settings.max_parallel_tasks == 4
    assert settings.workstation_cpu == 1.5
    assert settings.workstation_mem == "512m"
    assert settings.task_timeout_min == 45
    assert settings.llm_max_attempts == 5
    assert settings.llm_cache_ttl_days == 14
    assert settings.groq_api_key is not None
    assert settings.groq_api_key.get_secret_value() == "gsk_test123"


def test_prod_with_debug_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("DEBUG", "true")

    with pytest.raises(ValidationError, match="DEBUG=true is not allowed when ENV=prod"):
        Settings(_env_file=None)


def test_prod_without_debug_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")

    assert Settings(_env_file=None).env is Environment.PROD


@pytest.mark.parametrize(
    ("env", "expected"),
    [("dev", False), ("staging", False), ("prod", True)],
)
def test_deploy_approval_required_only_in_prod(
    monkeypatch: pytest.MonkeyPatch, env: str, expected: bool
) -> None:
    monkeypatch.setenv("ENV", env)

    assert Settings(_env_file=None).deploy_requires_approval is expected


def test_invalid_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "local")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", ["4", "gb", "0g", "4 g", ""])
def test_invalid_workstation_mem_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("WORKSTATION_MEM", value)

    with pytest.raises(ValidationError, match="WORKSTATION_MEM"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MAX_TASK_RETRIES", "-1"),
        ("MAX_TASK_RETRIES", "11"),
        ("MAX_PARALLEL_TASKS", "0"),
        ("WORKSTATION_CPU", "0"),
        ("TASK_TIMEOUT_MIN", "0"),
        ("LLM_MAX_ATTEMPTS", "0"),
        ("LLM_MAX_ATTEMPTS", "11"),
        ("LLM_CACHE_TTL_DAYS", "0"),
    ],
)
def test_out_of_range_limits_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_blank_api_key_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("CEREBRAS_API_KEY", "   ")

    settings = Settings(_env_file=None)

    assert settings.groq_api_key is None
    assert settings.cerebras_api_key is None


def test_secret_values_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_should_not_leak")

    settings = Settings(_env_file=None)

    assert "gsk_should_not_leak" not in repr(settings)
    assert "gsk_should_not_leak" not in str(settings.groq_api_key)


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
