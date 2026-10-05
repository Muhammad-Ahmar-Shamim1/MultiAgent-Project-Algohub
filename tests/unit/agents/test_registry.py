"""agents/registry.py: role -> BaseWorker subclass, parameterized by that role's blueprint."""

from web_department.agents.architect.agent import ArchitectWorker
from web_department.agents.base import BaseWorker
from web_department.agents.qa.agent import QAWorker
from web_department.agents.registry import build
from web_department.agents.security.agent import SecurityWorker
from web_department.blueprints.registry import BLUEPRINTS_DIR, BlueprintRegistry, load_all
from web_department.config.permissions import Role


def _real_registry() -> BlueprintRegistry:
    found = load_all(BLUEPRINTS_DIR)
    return BlueprintRegistry([spec for spec, _raw, _checksum in found.values()])


def test_build_maps_every_role_to_a_class() -> None:
    workers = build(_real_registry())

    assert set(workers) == set(Role)


def test_every_mapped_class_is_a_baseworker_subclass() -> None:
    workers = build(_real_registry())

    for worker_class in workers.values():
        assert issubclass(worker_class, BaseWorker)
        assert worker_class is not BaseWorker


def test_each_class_carries_its_own_role_and_system_prompt() -> None:
    registry = _real_registry()
    workers = build(registry)

    backend_worker = workers[Role.BACKEND]
    qa_worker = workers[Role.QA]

    assert backend_worker.role is Role.BACKEND
    assert backend_worker.system_prompt_name == registry.get(Role.BACKEND).system_prompt_template
    assert qa_worker.role is Role.QA
    assert qa_worker.system_prompt_name == registry.get(Role.QA).system_prompt_template
    assert backend_worker is not qa_worker


def test_purpose_comes_from_the_blueprint_s_first_preferred_model() -> None:
    registry = _real_registry()
    workers = build(registry)

    for role, worker_class in workers.items():
        assert worker_class.purpose == registry.get(role).models[0]


def test_the_architect_role_gets_the_specialised_architect_worker() -> None:
    workers = build(_real_registry())

    assert issubclass(workers[Role.ARCHITECT], ArchitectWorker)
    assert not issubclass(workers[Role.BACKEND], ArchitectWorker)


def test_the_qa_role_gets_the_specialised_qa_worker() -> None:
    workers = build(_real_registry())

    assert issubclass(workers[Role.QA], QAWorker)


def test_the_security_role_gets_the_security_worker_always_confidential() -> None:
    workers = build(_real_registry())

    assert issubclass(workers[Role.SECURITY], SecurityWorker)
    assert workers[Role.SECURITY].confidential is True


def test_evaluation_checks_come_from_the_blueprint() -> None:
    registry = _real_registry()
    workers = build(registry)

    for role, worker_class in workers.items():
        assert worker_class.evaluation_checks == tuple(registry.get(role).evaluation)
    assert workers[Role.ARCHITECT].evaluation_checks == ("openapi_valid",)
