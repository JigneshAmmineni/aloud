"""create_artifact tool contracts (FR-12, SDD §2.5)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from agent.tools import ARTIFACT_KINDS, make_create_artifact_handler, tool_schemas
from db.models import Artifact, Base, Session, User


async def _make_db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(User(id="local-user"))
        db.add(Session(id="s-1", user_id="local-user", status="active"))
        await db.commit()
    return factory


def _params(arguments: dict):
    params = MagicMock()
    params.arguments = arguments
    params.llm.push_frame = AsyncMock()
    params.result_callback = AsyncMock()
    return params


def test_tool_schema_is_registered_with_required_fields():
    schema = tool_schemas().standard_tools[0]
    assert schema.name == "create_artifact"
    assert set(schema.required) == {"title", "kind", "content"}
    assert set(schema.properties["kind"]["enum"]) == set(ARTIFACT_KINDS)


def test_create_artifact_writes_row_emits_event_and_confirms():
    async def run():
        factory = await _make_db()
        handler = make_create_artifact_handler("s-1", factory)
        params = _params(
            {"title": "Pricing plan", "kind": "summary", "content": "1. Charge more."}
        )
        await handler(params)

        async with factory() as db:
            rows = (await db.execute(select(Artifact))).scalars().all()
        assert len(rows) == 1
        assert rows[0].title == "Pricing plan"
        assert rows[0].session_id == "s-1"
        assert rows[0].user_id == "local-user"

        frame = params.llm.push_frame.call_args.args[0]
        assert isinstance(frame, RTVIServerMessageFrame)
        assert frame.data["type"] == "artifact.created"
        assert frame.data["artifact"]["title"] == "Pricing plan"
        assert frame.data["artifact"]["content"] == "1. Charge more."

        result = params.result_callback.call_args.args[0]
        assert result["status"] == "created"

    asyncio.run(run())


def test_unknown_kind_is_coerced_to_summary():
    async def run():
        factory = await _make_db()
        handler = make_create_artifact_handler("s-1", factory)
        params = _params({"title": "T", "kind": "haiku", "content": "body"})
        await handler(params)
        async with factory() as db:
            row = (await db.execute(select(Artifact))).scalars().one()
        assert row.kind == "summary"

    asyncio.run(run())


def test_empty_content_is_rejected_without_row_or_event():
    async def run():
        factory = await _make_db()
        handler = make_create_artifact_handler("s-1", factory)
        params = _params({"title": "T", "kind": "summary", "content": "   "})
        await handler(params)

        async with factory() as db:
            rows = (await db.execute(select(Artifact))).scalars().all()
        assert rows == []
        params.llm.push_frame.assert_not_called()
        assert params.result_callback.call_args.args[0]["status"] == "error"

    asyncio.run(run())


def test_db_failure_returns_error_result_without_raising():
    class BrokenFactory:
        def __call__(self):
            raise RuntimeError("db down")

    async def run():
        handler = make_create_artifact_handler("s-1", BrokenFactory())
        params = _params({"title": "T", "kind": "summary", "content": "body"})
        await handler(params)  # must not raise
        assert params.result_callback.call_args.args[0]["status"] == "error"
        params.llm.push_frame.assert_not_called()

    asyncio.run(run())
