"""create_artifact tool contracts (FR-12)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from sqlalchemy import select

import agent.tools as tools
from agent.tools import ARTIFACT_KINDS, make_create_artifact_handler, tool_schemas
from db.engine import init_db, session_factory
from db.models import Artifact
from db.sessions_repo import create_session_row
from db.users_repo import provision_user


async def _setup_db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/tools_test.db")
    await provision_user("uid-a", None)
    await create_session_row("s-1", "uid-a")


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


def test_create_artifact_writes_row_emits_event_and_confirms(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        handler = make_create_artifact_handler("s-1", "uid-a")
        params = _params(
            {"title": "Pricing plan", "kind": "summary", "content": "1. Charge more."}
        )
        await handler(params)

        async with session_factory()() as db:
            rows = (await db.execute(select(Artifact))).scalars().all()
        assert len(rows) == 1
        assert rows[0].title == "Pricing plan"
        assert rows[0].session_id == "s-1"
        assert rows[0].user_id == "uid-a"

        frame = params.llm.push_frame.call_args.args[0]
        assert isinstance(frame, RTVIServerMessageFrame)
        assert frame.data["type"] == "artifact.created"
        assert frame.data["artifact"]["title"] == "Pricing plan"
        assert frame.data["artifact"]["content"] == "1. Charge more."

        result = params.result_callback.call_args.args[0]
        assert result["status"] == "created"

    asyncio.run(run())


def test_unknown_kind_is_coerced_to_summary(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        handler = make_create_artifact_handler("s-1", "uid-a")
        params = _params({"title": "T", "kind": "haiku", "content": "body"})
        await handler(params)
        async with session_factory()() as db:
            row = (await db.execute(select(Artifact))).scalars().one()
        assert row.kind == "summary"

    asyncio.run(run())


def test_empty_content_is_rejected_without_row_or_event(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        handler = make_create_artifact_handler("s-1", "uid-a")
        params = _params({"title": "T", "kind": "summary", "content": "   "})
        await handler(params)

        async with session_factory()() as db:
            rows = (await db.execute(select(Artifact))).scalars().all()
        assert rows == []
        params.llm.push_frame.assert_not_called()
        assert params.result_callback.call_args.args[0]["status"] == "error"

    asyncio.run(run())


def test_db_failure_returns_error_result_without_raising(monkeypatch):
    def broken_scope(user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(tools, "user_scoped_session", broken_scope)

    async def run():
        handler = make_create_artifact_handler("s-1", "uid-a")
        params = _params({"title": "T", "kind": "summary", "content": "body"})
        await handler(params)  # must not raise
        assert params.result_callback.call_args.args[0]["status"] == "error"
        params.llm.push_frame.assert_not_called()

    asyncio.run(run())
