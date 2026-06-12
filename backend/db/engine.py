"""Async engine + boot-time init: create tables, seed the stub user.

MVP runs single-user (auth TBD, SDD §11): every session belongs to
DEFAULT_USER_ID until auth lands.
"""

from loguru import logger
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from db.models import Base, User

DEFAULT_USER_ID = "local-user"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker | None = None


def session_factory() -> async_sessionmaker:
    assert _session_factory is not None, "init_db() must run first"
    return _session_factory


async def init_db(database_url: str) -> None:
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with _session_factory() as db:
        if _engine.dialect.name == "postgresql":
            await db.execute(
                pg_insert(User)
                .values(id=DEFAULT_USER_ID)
                .on_conflict_do_nothing(index_elements=["id"])
            )
        else:  # sqlite in tests
            if await db.get(User, DEFAULT_USER_ID) is None:
                db.add(User(id=DEFAULT_USER_ID))
        await db.commit()

    logger.bind(component="db", event="db.initialized").info(
        f"Database ready ({_engine.dialect.name})"
    )
