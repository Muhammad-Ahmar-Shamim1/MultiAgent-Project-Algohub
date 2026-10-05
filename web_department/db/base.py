"""Async engine, session factory and declarative base for the platform database."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from functools import lru_cache
from typing import ClassVar

from sqlalchemy import DateTime, MetaData, String, event, func
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, declared_attr, mapped_column
from sqlalchemy.orm.session import SessionTransaction
from ulid import ULID

from web_department.config.settings import get_settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


PENDING_EVENTS_KEY = "pending_events"


def new_id(prefix: str) -> str:
    """Return a time-sortable prefixed ULID such as ``prj_01J8...``."""
    return f"{prefix}_{ULID()}"


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base for every table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IdMixin:
    """Primary key ``id`` as a prefixed ULID; subclasses set ``id_prefix``."""

    id_prefix: ClassVar[str]

    @declared_attr
    def id(cls) -> Mapped[str]:  # noqa: N805
        prefix = cls.id_prefix
        return mapped_column(String(32), primary_key=True, default=lambda: new_id(prefix))


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Entity(IdMixin, TimestampMixin, Base):
    """Base for mutable aggregates: prefixed ULID id plus created_at/updated_at."""

    __abstract__ = True


def create_engine(url: str) -> AsyncEngine:
    """Create an async engine (pgvector values are serialized by the SQLAlchemy ``Vector`` type)."""
    return create_async_engine(url, pool_pre_ping=True)


@lru_cache
def get_engine() -> AsyncEngine:
    """Return the process-wide engine built from ``Settings.database_url``."""
    return create_engine(get_settings().database_url)


def get_session_factory(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to ``engine`` (default: the process-wide engine)."""
    return async_sessionmaker(engine or get_engine(), expire_on_commit=False)


@event.listens_for(Session, "after_soft_rollback")
def _drop_pending_events(session: Session, _previous: SessionTransaction) -> None:
    """Events queued for streaming belong to the rolled-back work; never stream them."""
    session.info.pop(PENDING_EVENTS_KEY, None)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session; commit on success (then stream its events), roll back on error."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        if session.info.get(PENDING_EVENTS_KEY):
            from web_department.collaboration.events import get_event_bus

            await get_event_bus().dispatch_pending(session)
