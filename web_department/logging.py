"""Structured JSON logging with project/task/agent context (SRS NFR-012)."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from structlog.typing import FilteringBoundLogger


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog to emit one JSON object per line on stdout."""
    numeric_level = logging.getLevelNamesMapping()[level.upper()]
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """Return a structlog logger; bound context vars appear on every record."""
    logger: FilteringBoundLogger = structlog.get_logger(name)
    return logger


@contextmanager
def log_context(
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> Iterator[None]:
    """Attach project/task/agent IDs to every log line emitted inside the block."""
    values = {
        key: value
        for key, value in (
            ("project_id", project_id),
            ("task_id", task_id),
            ("agent_id", agent_id),
        )
        if value is not None
    }
    with structlog.contextvars.bound_contextvars(**values):
        yield
