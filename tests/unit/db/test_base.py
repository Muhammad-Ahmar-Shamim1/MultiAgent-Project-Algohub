"""Tests for ID generation and model conventions that need no database."""

import re

from sqlalchemy import inspect

from web_department.db import models
from web_department.db.base import Base, Entity, create_engine, new_id

ULID_ID = re.compile(r"^[a-z]{3}_[0-9A-HJKMNP-TV-Z]{26}$")

ENTITY_PREFIXES = {
    models.Project: "prj",
    models.RequirementSpec: "spc",
    models.Workstation: "wks",
    models.Agent: "agt",
    models.Task: "tsk",
    models.Event: "evt",
    models.Approval: "apr",
    models.Deployment: "dpl",
    models.KnowledgeEntry: "knw",
    models.LLMCall: "llm",
}


def test_new_id_is_prefixed_ulid_and_unique() -> None:
    ids = {new_id("prj") for _ in range(100)}

    assert len(ids) == 100
    assert all(ULID_ID.match(i) for i in ids)


def test_each_model_has_its_own_id_prefix() -> None:
    for model, prefix in ENTITY_PREFIXES.items():
        assert model.id_prefix == prefix
    assert len(set(ENTITY_PREFIXES.values())) == len(ENTITY_PREFIXES)


def test_default_id_uses_model_prefix() -> None:
    column = inspect(models.Task).columns["id"]

    assert column.default is not None
    assert column.default.arg(None).startswith("tsk_")  # type: ignore[attr-defined]


def test_all_srs_entities_have_tables() -> None:
    assert {t for t in Base.metadata.tables} >= {
        "projects",
        "requirement_specs",
        "tasks",
        "task_dependencies",
        "blueprints",
        "agents",
        "workstations",
        "events",
        "approvals",
        "deployments",
        "knowledge_entries",
        "llm_calls",
    }


def test_only_events_and_approvals_are_append_only() -> None:
    flagged = {m.__name__ for m in ENTITY_PREFIXES if getattr(m, "__append_only__", False)}

    assert flagged == {"Event", "Approval"}


def test_append_only_tables_have_no_updated_at() -> None:
    for model in (models.Event, models.Approval):
        assert "updated_at" not in inspect(model).columns
    assert issubclass(models.Project, Entity)
    assert "updated_at" in inspect(models.Project).columns


def test_knowledge_embedding_dimension_is_768() -> None:
    assert inspect(models.KnowledgeEntry).columns["embedding"].type.dim == 768  # type: ignore[attr-defined]


def test_create_engine_does_not_connect() -> None:
    engine = create_engine("postgresql+asyncpg://u:p@localhost:1/none")

    assert engine.url.database == "none"
