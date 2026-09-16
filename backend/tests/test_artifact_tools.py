"""FR-45 tool registry + artifacts repo contracts.

Covers the registry shape, the four handlers through real (sqlite) DB
rows, the two mandated data-loss rules — (i) concurrent appends lose
neither edit, (ii) one create plus N edits aggregates as 1 count / N
edits — and NFR-8's negative (another user's artifact id reads as
not-found) for read AND edit.
"""

import asyncio

from sqlalchemy import select

import agent.tools as tools
from agent.tools import (
    READ_ARTIFACT_MAX_CHARS,
    ToolContext,
    build_registry,
    registry_schemas,
)
from db.artifacts_repo import LIST_ARTIFACTS_CAP
from db.engine import init_db, session_factory
from db.models import Artifact, UsageEvent
from db.sessions_repo import create_session_row
from db.users_repo import provision_user


async def _setup_db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/artifact_tools.db")
    await provision_user("uid-a", None)
    await provision_user("uid-b", None)
    await create_session_row("s-a", "uid-a")
    await create_session_row("s-b", "uid-b")


def _ctx(emitted: list, user_id="uid-a", session_id="s-a", turn_id=3):
    async def emit(data):
        emitted.append(data)

    return ToolContext(
        session_id=session_id, user_id=user_id, turn_id=turn_id, emit=emit
    )


def _tool(name):
    return next(t for t in build_registry() if t.name == name)


async def _create(ctx, title="T", content="body", kind="summary"):
    result = await _tool("create_artifact").handler(
        {"title": title, "kind": kind, "content": content}, ctx
    )
    assert result["status"] == "created"
    return result["artifact_id"]


# ---- registry shape ----


def test_registry_inventory_and_classification():
    registry = build_registry()
    flags = {t.name: t.is_write for t in registry}
    assert flags == {
        "create_artifact": True,
        "list_artifacts": False,
        "read_artifact": False,
        "edit_artifact": True,
    }
    schemas = registry_schemas(registry)
    assert [s["name"] for s in schemas] == list(flags)
    edit = next(s for s in schemas if s["name"] == "edit_artifact")
    assert set(edit["parameters"]["required"]) == {"artifact_id", "mode", "content"}
    assert edit["parameters"]["properties"]["mode"]["enum"] == ["replace", "append"]


# ---- create ----


def test_create_writes_row_event_announce_and_turn_attribution(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        emitted = []
        artifact_id = await _create(
            _ctx(emitted, turn_id=7), title="Plan", content="1. Charge more."
        )

        async with session_factory()() as db:
            row = (await db.execute(select(Artifact))).scalars().one()
            events = (await db.execute(select(UsageEvent).where(UsageEvent.stage == "artifact"))).scalars().all()
        assert row.id == artifact_id and row.user_id == "uid-a"
        assert [(e.unit, e.turn_id, e.detail) for e in events] == [
            ("count", 7, "summary")
        ]
        assert emitted[0]["type"] == "artifact.created"
        assert emitted[0]["artifact"]["content"] == "1. Charge more."

    asyncio.run(run())


# ---- list ----


def test_list_orders_by_recent_activity_and_excludes_content(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        emitted = []
        ctx = _ctx(emitted)
        first = await _create(ctx, title="First")
        second = await _create(ctx, title="Second")
        # editing the OLDER artifact surfaces it (COALESCE(updated_at, created_at))
        result = await _tool("edit_artifact").handler(
            {"artifact_id": first, "mode": "append", "content": "more"}, ctx
        )
        assert result["status"] == "edited"

        listing = await _tool("list_artifacts").handler({}, ctx)
        assert [a["id"] for a in listing["artifacts"]] == [first, second]
        assert listing["count"] == 2
        assert "content" not in listing["artifacts"][0]

    asyncio.run(run())


def test_list_is_capped(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        for i in range(LIST_ARTIFACTS_CAP + 1):
            await _create(ctx, title=f"A{i}")
        listing = await _tool("list_artifacts").handler({}, ctx)
        assert listing["count"] == LIST_ARTIFACTS_CAP

    asyncio.run(run())


# ---- read ----


def test_read_returns_content_and_truncates_past_cap(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        small = await _create(ctx, content="short body")
        big = await _create(ctx, content="x" * (READ_ARTIFACT_MAX_CHARS + 500))

        got = await _tool("read_artifact").handler({"artifact_id": small}, ctx)
        assert (got["content"], got["truncated"]) == ("short body", False)

        got = await _tool("read_artifact").handler({"artifact_id": big}, ctx)
        assert got["truncated"] is True
        assert got["content"].endswith("[truncated]")
        assert len(got["content"]) < READ_ARTIFACT_MAX_CHARS + 100

        missing = await _tool("read_artifact").handler({"artifact_id": 9999}, ctx)
        assert missing["status"] == "not_found"

    asyncio.run(run())


# ---- edit ----


def test_edit_append_and_replace_update_row_and_announce_upsert(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        emitted = []
        ctx = _ctx(emitted, turn_id=5)
        artifact_id = await _create(ctx, content="v1")

        result = await _tool("edit_artifact").handler(
            {"artifact_id": artifact_id, "mode": "append", "content": "v2"}, ctx
        )
        assert result["status"] == "edited"
        result = await _tool("edit_artifact").handler(
            {
                "artifact_id": artifact_id,
                "mode": "replace",
                "content": "clean",
                "title": "Renamed",
            },
            ctx,
        )
        assert result["status"] == "edited"

        async with session_factory()() as db:
            row = (await db.execute(select(Artifact))).scalars().one()
        assert (row.content, row.title) == ("clean", "Renamed")
        assert row.updated_at is not None

        updates = [e for e in emitted if e["type"] == "artifact.updated"]
        assert updates[0]["artifact"]["content"] == "v1\nv2"  # post-edit payload
        assert updates[1]["artifact"]["title"] == "Renamed"

    asyncio.run(run())


def test_replace_refused_past_read_cap_append_still_works(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        big = await _create(ctx, content="x" * (READ_ARTIFACT_MAX_CHARS + 1))
        result = await _tool("edit_artifact").handler(
            {"artifact_id": big, "mode": "replace", "content": "tiny"}, ctx
        )
        assert result["status"] == "refused"
        assert "append" in result["error"]
        result = await _tool("edit_artifact").handler(
            {"artifact_id": big, "mode": "append", "content": "tail"}, ctx
        )
        assert result["status"] == "edited"

    asyncio.run(run())


def test_concurrent_appends_lose_neither(tmp_path):
    """Mandated (i): the atomic DB-side concatenation — two appends racing
    (as when a write outlives its turn, FR-46) both land."""

    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        artifact_id = await _create(ctx, content="base")
        edit = _tool("edit_artifact").handler
        await asyncio.gather(
            edit({"artifact_id": artifact_id, "mode": "append", "content": "left"}, ctx),
            edit({"artifact_id": artifact_id, "mode": "append", "content": "right"}, ctx),
        )
        async with session_factory()() as db:
            content = (await db.execute(select(Artifact.content))).scalar_one()
        assert "left" in content and "right" in content and content.startswith("base")

    asyncio.run(run())


def test_one_create_n_edits_aggregates_one_count_n_edits(tmp_path):
    """Mandated (ii): never N+1 artifacts."""

    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        artifact_id = await _create(ctx)
        for i in range(3):
            await _tool("edit_artifact").handler(
                {"artifact_id": artifact_id, "mode": "append", "content": f"e{i}"},
                ctx,
            )
        async with session_factory()() as db:
            artifacts = (await db.execute(select(Artifact))).scalars().all()
            events = (
                (await db.execute(select(UsageEvent).where(UsageEvent.stage == "artifact")))
                .scalars()
                .all()
            )
        assert len(artifacts) == 1
        units = sorted(e.unit for e in events)
        assert units == ["count", "edits", "edits", "edits"]

    asyncio.run(run())


# ---- NFR-8 negative ----


def test_another_users_artifact_reads_as_not_found(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        theirs = await _create(_ctx([], user_id="uid-b", session_id="s-b"))
        mine = _ctx([], user_id="uid-a", session_id="s-a")

        got = await _tool("read_artifact").handler({"artifact_id": theirs}, mine)
        assert got["status"] == "not_found"
        listing = await _tool("list_artifacts").handler({}, mine)
        assert theirs not in [a["id"] for a in listing["artifacts"]]
        for mode in ("replace", "append"):
            got = await _tool("edit_artifact").handler(
                {"artifact_id": theirs, "mode": mode, "content": "hijack"}, mine
            )
            assert got["status"] == "not_found"
        # and the row is untouched
        async with session_factory()() as db:
            content = (await db.execute(select(Artifact.content))).scalar_one()
        assert "hijack" not in content

    asyncio.run(run())


# ---- failure discipline ----


def test_bad_arguments_return_error_results(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        edit = _tool("edit_artifact").handler
        assert (await edit({"artifact_id": "x", "mode": "append", "content": "c"}, ctx))["status"] == "error"
        assert (await edit({"artifact_id": 1, "mode": "rewrite", "content": "c"}, ctx))["status"] == "error"
        assert (await edit({"artifact_id": 1, "mode": "append", "content": " "}, ctx))["status"] == "error"
        create = _tool("create_artifact").handler
        assert (await create({"title": "T", "kind": "summary", "content": ""}, ctx))["status"] == "error"

    asyncio.run(run())


def test_db_failure_returns_error_result_without_raising(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(tools, "create_artifact_row", broken)
    monkeypatch.setattr(tools, "append_artifact_content", broken)

    async def run():
        emitted = []
        ctx = _ctx(emitted)
        create = await _tool("create_artifact").handler(
            {"title": "T", "kind": "summary", "content": "body"}, ctx
        )
        assert create["status"] == "error"
        edit = await _tool("edit_artifact").handler(
            {"artifact_id": 1, "mode": "append", "content": "c"}, ctx
        )
        assert edit["status"] == "error"
        assert emitted == []  # no announce for a failed write

    asyncio.run(run())


def test_unknown_kind_is_coerced_to_summary(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        await _tool("create_artifact").handler(
            {"title": "T", "kind": "haiku", "content": "body"}, ctx
        )
        async with session_factory()() as db:
            row = (await db.execute(select(Artifact))).scalars().one()
        assert row.kind == "summary"

    asyncio.run(run())


def test_edit_event_files_under_the_editing_session(tmp_path):
    """Round-2 blocking 1: the tool's defining case edits a PRIOR-session
    artifact — the artifact_edited event must carry the EDITING session
    (whose turn number it holds), or the cost vanishes from the current
    drill-down and pollutes the old session with a phantom turn."""

    async def run():
        await _setup_db(tmp_path)
        await create_session_row("s-a2", "uid-a")  # today's session
        created_in = _ctx([], session_id="s-a")  # yesterday's
        artifact_id = await _create(created_in, title="Old plan")

        editing = _ctx([], session_id="s-a2", turn_id=9)
        result = await _tool("edit_artifact").handler(
            {"artifact_id": artifact_id, "mode": "append", "content": "new bullet"},
            editing,
        )
        assert result["status"] == "edited"

        async with session_factory()() as db:
            events = (
                (
                    await db.execute(
                        select(UsageEvent).where(UsageEvent.unit == "edits")
                    )
                )
                .scalars()
                .all()
            )
        assert [(e.session_id, e.turn_id) for e in events] == [("s-a2", 9)]

    asyncio.run(run())


def test_replace_cap_is_enforced_inside_the_update_statement(tmp_path):
    """Round-3 blocking 2: the cap-refusal must hold ATOMICALLY — a
    detached write can grow the row past the cap between the tool's read
    and the replace, and a replace that lands then deletes content the
    model never saw. The repo enforces the cap in the UPDATE's predicate."""
    from db.artifacts_repo import replace_artifact_content

    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        big = await _create(ctx, content="x" * (READ_ARTIFACT_MAX_CHARS + 1))

        # the repo layer refuses directly, no matter what the caller read
        row = await replace_artifact_content(
            "uid-a",
            big,
            "tiny",
            None,
            3,
            session_id="s-a",
            max_replaceable_chars=READ_ARTIFACT_MAX_CHARS,
        )
        assert row is None
        async with session_factory()() as db:
            content = (await db.execute(select(Artifact.content))).scalar_one()
        assert content.startswith("xxx") and len(content) > READ_ARTIFACT_MAX_CHARS

    asyncio.run(run())


def test_replace_race_where_row_grows_after_the_read_returns_refused(
    tmp_path, monkeypatch
):
    """The handler half of the same race: the pre-read saw a SMALL
    artifact (stale), the row grew past the cap before the UPDATE — the
    result must be the refusal steering the model to append, and the
    content must survive."""
    from types import SimpleNamespace

    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        big = await _create(ctx, content="x" * (READ_ARTIFACT_MAX_CHARS + 1))

        real_get = tools.get_artifact_row
        calls = {"n": 0}

        async def stale_first_read(user_id, artifact_id):
            calls["n"] += 1
            if calls["n"] == 1:  # the read that raced the detached write
                return SimpleNamespace(content="small")
            return await real_get(user_id, artifact_id)

        monkeypatch.setattr(tools, "get_artifact_row", stale_first_read)
        result = await _tool("edit_artifact").handler(
            {"artifact_id": big, "mode": "replace", "content": "tiny"}, ctx
        )
        assert result["status"] == "refused"
        async with session_factory()() as db:
            content = (await db.execute(select(Artifact.content))).scalar_one()
        assert "tiny" not in content

    asyncio.run(run())
