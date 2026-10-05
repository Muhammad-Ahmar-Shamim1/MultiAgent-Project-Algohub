"""Environment-based application settings (AGENTS.md rule 9)."""

import re
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from web_department.config.environments import Environment, get_profile

_DOCKER_MEM = re.compile(r"^[1-9]\d*[bkmg]$", re.IGNORECASE)


class Settings(BaseSettings):
    """Process-wide configuration, populated from environment variables / `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    env: Environment = Environment.DEV
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: str = "postgresql+asyncpg://algohub:changeme@localhost:5432/algohub"
    redis_url: str = "redis://localhost:6379/0"
    ollama_base_url: str = "http://localhost:11434"

    max_task_retries: int = Field(default=3, ge=0, le=10)
    max_parallel_tasks: int = Field(default=2, ge=1)
    # QA fix cycles per task before escalating (FR-103, qa/failure_triage.py): after this many,
    # the next cycles replan the task, then switch it to the strongest model, then BLOCK it for
    # human review.
    max_fix_cycles: int = Field(default=3, ge=1, le=10)
    # Agent Factory (FR-051): cap on live (non-RETIRED) agents of one role per project - acquire()
    # reuses an AVAILABLE one under this cap, or raises RoleCapReached once it's hit.
    max_agents_per_role: int = Field(default=3, ge=1)
    # Agent Factory watchdog (FR-053): a WORKING agent past its blueprint's budget.timeout_min for
    # longer than this many seconds is stuck, not just slow - BLOCKED and diagnosed.
    agent_watchdog_interval_seconds: float = Field(default=60.0, gt=0)
    workstation_cpu: float = Field(default=2, gt=0)
    workstation_mem: str = "4g"
    task_timeout_min: int = Field(default=30, ge=1)
    # Host-side root for per-project workspace bind mounts (FR-060): <dir>/<project_id>/.
    workstation_data_dir: str = "./data/workspaces"
    # Where War Room close() copies a project's artifacts (FR-080) before the workstation - and
    # its workspace - is destroyed: <dir>/<project_id>/<war_room_id>/<artifact path>.
    war_room_archive_dir: str = "./data/war_rooms"
    # IR-024: keep True unless you have explicitly decided a generated project's own code may go
    # to a shared/free-tier provider (it may train on prompts) - e.g. no private/local model is
    # available and you've knowingly accepted that trade-off for a specific validation run. The
    # default protects every other setting; this is the one place a human opts out per-checkout.
    coder_confidential: bool = True

    llm_max_attempts: int = Field(default=3, ge=1, le=10)
    llm_cache_ttl_days: int = Field(default=7, ge=1)
    # Hard safety cap on LLM calls a single worker may spend on one task (AGENTS.md rule 8),
    # independent of `max_task_retries` - a backstop for richer per-attempt loops later.
    max_llm_calls_per_task: int = Field(default=40, ge=1)

    # Hours ANALYZING may wait for clarification answers before proceeding with the LLM's stated
    # defaults (FR-022).
    analysis_clarification_timeout_hours: int = Field(default=24, ge=1)

    groq_api_key: SecretStr | None = None
    cerebras_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    github_models_token: SecretStr | None = None

    # Bearer tokens for the three human/system user classes (SRS 2.3). Env-based for now; a real
    # identity provider is future work.
    requester_token: SecretStr | None = None
    approver_token: SecretStr | None = None
    operator_token: SecretStr | None = None

    # Optional webhook the Web Agent posts project status changes to (FR-004).
    status_webhook_url: str | None = None
    # Optional chat webhook for approval requests (FR-130): a Discord or Slack *incoming* webhook
    # URL (both free). Secret: the URL itself is the credential.
    approval_webhook_url: SecretStr | None = None
    # Base URL put into notification messages so approvers can find the request.
    public_api_base_url: str = "http://localhost:8000"

    @field_validator(
        "groq_api_key",
        "cerebras_api_key",
        "openrouter_api_key",
        "github_models_token",
        "requester_token",
        "approver_token",
        "operator_token",
        "status_webhook_url",
        "approval_webhook_url",
        mode="before",
    )
    @classmethod
    def _blank_key_is_none(cls, value: object) -> object:
        """Treat `KEY=` (empty) in `.env` as unset."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("workstation_mem")
    @classmethod
    def _valid_docker_memory(cls, value: str) -> str:
        """Require a Docker memory string such as `512m` or `4g`."""
        if not _DOCKER_MEM.match(value):
            raise ValueError(f"WORKSTATION_MEM must look like '512m' or '4g', got {value!r}")
        return value

    @model_validator(mode="after")
    def _enforce_environment_rules(self) -> Self:
        """Reject settings the current environment forbids (e.g. DEBUG in prod)."""
        if self.debug and not get_profile(self.env).allow_debug:
            raise ValueError(
                f"DEBUG=true is not allowed when ENV={self.env.value}; set DEBUG=false"
            )
        return self

    @property
    def deploy_requires_approval(self) -> bool:
        """Whether deployments in this environment need human approval (FR-130)."""
        return get_profile(self.env).require_deploy_approval


@lru_cache
def get_settings() -> Settings:
    """Return the cached, process-wide `Settings` instance."""
    return Settings()
