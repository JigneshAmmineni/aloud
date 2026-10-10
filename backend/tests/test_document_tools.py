"""FR-53 tool registry + handler contracts (ordinary lane).

Covers the registry shape, the handlers through real (sqlite) DB rows, the
two mandated data-loss rules — (i) concurrent appends lose neither edit,
(ii) one create plus N edits aggregates as 1 count / N edits — NFR-8's
negative for read AND edit, the steering messages' shapes, and the announce
payload cap. The dialect-bound internals (insert SQL, search SQL) are
covered in test_documents_pg.py; here the search handlers run against
monkeypatched repo functions to pin the tool-result shape.
"""

import asyncio

from sqlalchemy import select

import agent.registry as registry_mod
import agent.tools as tools
from agent.tools import (
    ToolContext,
    build_registry,
    registry_schemas,
)
from db.documents_repo import (
    LIST_DOCUMENTS_CAP,
    READ_DOCUMENT_MAX_CHARS,
    DocumentSearchMatch,
    DocumentSearchResult,
    WorkspaceSearchHit,
    WorkspaceSearchResult,
    insert_upload_row,
)
from db.engine import init_db, session_factory
from db.models import Document, UsageEvent
from db.sessions_repo import create_session_row
from db.users_repo import provision_user


async def _setup_db(tmp_path):
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/document_tools.db")
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
    result = await _tool("create_document").handler(
        {"title": title, "kind": kind, "content": content}, ctx
    )
    assert result["status"] == "created"
    return result["document_id"]


# ---- registry shape ----


def test_registry_inventory_and_classification():
    registry = build_registry()
    flags = {t.name: t.is_write for t in registry}
    assert flags == {
        "create_document": True,
        "list_documents": False,
        "read_document": False,
        "search_documents": False,
        "edit_document": True,
    }
    schemas = registry_schemas(registry)
    assert [s["name"] for s in schemas] == list(flags)
    edit = next(s for s in schemas if s["name"] == "edit_document")
    assert set(edit["parameters"]["required"]) == {"document_id", "mode"}
    assert edit["parameters"]["properties"]["mode"]["enum"] == [
        "str_replace",
        "insert",
        "append",
        "replace",
    ]


# ---- create ----


def test_create_writes_row_event_announce_and_turn_attribution(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        emitted = []
        document_id = await _create(
            _ctx(emitted, turn_id=7), title="Plan", content="1. Charge more."
        )

        async with session_factory()() as db:
            row = (await db.execute(select(Document))).scalars().one()
            events = (
                (
                    await db.execute(
                        select(UsageEvent).where(UsageEvent.stage == "artifact")
                    )
                )
                .scalars()
                .all()
            )
        assert row.id == document_id and row.user_id == "uid-a"
        assert (row.source, row.format) == ("agent", "markdown")
        assert [(e.unit, e.turn_id, e.detail) for e in events] == [
            ("count", 7, "summary")
        ]
        assert emitted[0]["type"] == "document.created"
        doc = emitted[0]["document"]
        assert doc["content"] == "1. Charge more."
        assert doc["content_omitted"] is False
        assert doc["char_count"] == len("1. Charge more.")

    asyncio.run(run())


def test_create_refuses_over_ceiling_and_quota(tmp_path, monkeypatch):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        monkeypatch.setattr(tools, "MAX_DOC_CHARS", 10)
        result = await _tool("create_document").handler(
            {"title": "T", "kind": "summary", "content": "x" * 11}, ctx
        )
        assert result["status"] == "refused" and "size limit" in result["error"]

        import db.documents_repo as repo

        monkeypatch.setattr(repo, "MAX_DOCUMENTS_PER_USER", 1)
        monkeypatch.setattr(tools, "MAX_DOC_CHARS", 10_000)
        await _create(ctx)
        result = await _tool("create_document").handler(
            {"title": "T2", "kind": "summary", "content": "body"}, ctx
        )
        assert result["status"] == "refused" and "limit" in result["error"]

    asyncio.run(run())


# ---- list ----


def test_list_includes_both_sources_ordered_and_capped(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        created = await _create(ctx, title="Created")
        uploaded = await insert_upload_row("uid-a", "up.md", "markdown", "u")
        listing = await _tool("list_documents").handler({}, ctx)
        by_id = {d["id"]: d for d in listing["documents"]}
        assert by_id[created]["source"] == "agent"
        assert by_id[uploaded.id]["source"] == "uploaded"
        assert "content" not in by_id[created]
        assert by_id[uploaded.id]["char_count"] == 1

        for i in range(LIST_DOCUMENTS_CAP):
            await _create(ctx, title=f"A{i}")
        listing = await _tool("list_documents").handler({}, ctx)
        assert listing["count"] == LIST_DOCUMENTS_CAP
        assert listing["total"] == LIST_DOCUMENTS_CAP + 2

    asyncio.run(run())


# ---- read (paged) ----


def test_read_returns_numbered_page_and_steers_out_of_range(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        doc = await _create(ctx, content="alpha\nbeta")
        got = await _tool("read_document").handler({"document_id": doc}, ctx)
        assert got["content"] == "1: alpha\n2: beta"
        assert (got["page"], got["total_pages"]) == (1, 1)
        assert "truncated" not in got  # marker only when further pages exist
        assert "prefix" in got["note"]  # the decoration warning

        big = await _create(ctx, content="x\n" * (READ_DOCUMENT_MAX_CHARS))
        got = await _tool("read_document").handler({"document_id": big}, ctx)
        assert got["total_pages"] > 1
        assert "page=2" in got["truncated"]
        page2 = await _tool("read_document").handler(
            {"document_id": big, "page": 2}, ctx
        )
        assert page2["first_line"] == got["last_line"] + 1  # numbers continue

        oob = await _tool("read_document").handler(
            {"document_id": doc, "page": 9}, ctx
        )
        assert oob["status"] == "error" and "out of range" in oob["error"]
        missing = await _tool("read_document").handler({"document_id": 9999}, ctx)
        assert missing["status"] == "not_found"

    asyncio.run(run())


# ---- search (tool shape; SQL covered in the Postgres lane) ----


def test_search_requires_a_term_and_shapes_both_scopes(monkeypatch):
    async def run():
        ctx = _ctx([])
        search = _tool("search_documents").handler
        assert (await search({"query": "   "}, ctx))["status"] == "error"

        async def fake_workspace(user_id, query):
            return WorkspaceSearchResult(
                hits=[
                    WorkspaceSearchHit(
                        id=1, title="plan.md", kind=None, format="markdown",
                        match="content", title_matched=True, line=4,
                        snippet="the plan", match_count=3,
                    )
                ],
                documents_matched=12,
                scanned=50,
                scan_truncated=True,
            )

        monkeypatch.setattr(tools, "search_workspace", fake_workspace)
        out = await search({"query": "plan"}, ctx)
        assert out["scope"] == "workspace"
        assert out["results"][0]["match"] == "content"
        assert out["results"][0]["title_matched"] is True
        assert out["documents_matched"] == 12
        assert "partial" in out["note"] and "most recent" in out["note"]

        async def fake_single(user_id, document_id, query):
            return DocumentSearchResult(
                matches=[DocumentSearchMatch(line=2, snippet="## Plan")],
                total_matches=4,
                exact_matches=3,
            )

        monkeypatch.setattr(tools, "search_document", fake_single)
        out = await search({"query": "plan", "document_id": 1}, ctx)
        assert out["scope"] == "document"
        assert (out["total_matches"], out["exact_matches"]) == (4, 3)
        assert "exact_matches" in out["note"]  # the transfer boundary

    asyncio.run(run())


# ---- edit: str_replace ----


def test_str_replace_validation_success_and_steering(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        emitted = []
        ctx = _ctx(emitted)
        doc = await _create(ctx, content="plan A\nplan B\ndone")
        edit = _tool("edit_document").handler

        # validation: old_str, replace_all's expected_occurrences, the floor
        assert (
            await edit({"document_id": doc, "mode": "str_replace"}, ctx)
        )["status"] == "error"
        r = await edit(
            {
                "document_id": doc, "mode": "str_replace",
                "old_str": "plan", "new_str": "x", "replace_all": True,
            },
            ctx,
        )
        assert r["status"] == "error" and "expected_occurrences" in r["error"]
        r = await edit(
            {
                "document_id": doc, "mode": "str_replace",
                "old_str": "plan", "new_str": "x",
                "replace_all": True, "expected_occurrences": 0,
            },
            ctx,
        )
        assert r["status"] == "error" and "at least 1" in r["error"]

        # ambiguous: refused with the count named
        r = await edit(
            {
                "document_id": doc, "mode": "str_replace",
                "old_str": "plan", "new_str": "idea",
            },
            ctx,
        )
        assert r["status"] == "refused" and "2 occurrences" in r["error"]

        # no match: the echoed old_str is bounded
        r = await edit(
            {
                "document_id": doc, "mode": "str_replace",
                "old_str": "z" * 500, "new_str": "x",
            },
            ctx,
        )
        assert r["status"] == "refused"
        assert "did not appear verbatim" in r["error"]
        assert len(r["error"]) < 400  # 200-char echo + fixed text

        # success: unique match
        r = await edit(
            {
                "document_id": doc, "mode": "str_replace",
                "old_str": "done", "new_str": "finished",
            },
            ctx,
        )
        assert r["status"] == "edited"
        updates = [e for e in emitted if e["type"] == "document.updated"]
        assert updates[-1]["document"]["content"].endswith("finished")

        # replace_all with the right count
        r = await edit(
            {
                "document_id": doc, "mode": "str_replace",
                "old_str": "plan", "new_str": "idea",
                "replace_all": True, "expected_occurrences": 2,
            },
            ctx,
        )
        assert r["status"] == "edited"

    asyncio.run(run())


def test_edit_steers_on_pdf_and_announce_caps_content(tmp_path, monkeypatch):
    async def run():
        await _setup_db(tmp_path)
        emitted = []
        ctx = _ctx(emitted)
        pdf = await insert_upload_row("uid-a", "scan.pdf", "pdf", "pdf text")
        r = await _tool("edit_document").handler(
            {
                "document_id": pdf.id, "mode": "str_replace",
                "old_str": "pdf", "new_str": "x",
            },
            ctx,
        )
        assert r["status"] == "refused" and "read-only" in r["error"]

        # the announce payload cap: content omitted, char_count carried
        monkeypatch.setattr(tools, "ANNOUNCE_CONTENT_MAX_CHARS", 10)
        await _create(ctx, content="0123456789ABCDEF")
        created = emitted[-1]["document"]
        assert created["content_omitted"] is True
        assert created["content"] is None
        assert created["char_count"] == 16

    asyncio.run(run())


def test_edit_reconciles_registered_providers_cross_session(tmp_path):
    """FR-53.8: a successful edit notifies EVERY live session of the owner
    through the registry — including a second session with the same
    document attached; sessions without it ignore the notify."""

    class FakeProvider:
        def __init__(self):
            self.updates = []
            self.removed = []

        def update_document_section(self, doc_id, title, content):
            self.updates.append((doc_id, title, content))
            return True

        def remove_document_section(self, doc_id):
            self.removed.append(doc_id)
            return True

    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        doc = await _create(ctx, content="before")
        mine, other_tab, other_user = FakeProvider(), FakeProvider(), FakeProvider()
        registry_mod.register_provider("s-a", "uid-a", mine)
        registry_mod.register_provider("s-a2", "uid-a", other_tab)
        registry_mod.register_provider("s-b", "uid-b", other_user)
        try:
            r = await _tool("edit_document").handler(
                {
                    "document_id": doc, "mode": "str_replace",
                    "old_str": "before", "new_str": "after",
                },
                ctx,
            )
            assert r["status"] == "edited"
            assert [u[2] for u in mine.updates] == ["after"]
            assert [u[2] for u in other_tab.updates] == ["after"]
            assert other_user.updates == []  # never another user's sessions
        finally:
            for sid in ("s-a", "s-a2", "s-b"):
                registry_mod.unregister_provider(sid)

    asyncio.run(run())


# ---- edit: insert validation (SQL itself is Postgres-lane) ----


def test_insert_argument_validation(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        doc = await _create(ctx)
        edit = _tool("edit_document").handler
        assert (
            await edit({"document_id": doc, "mode": "insert", "text": "x"}, ctx)
        )["status"] == "error"
        assert (
            await edit(
                {"document_id": doc, "mode": "insert", "insert_line": -1, "text": "x"},
                ctx,
            )
        )["status"] == "error"
        assert (
            await edit(
                {"document_id": doc, "mode": "insert", "insert_line": 0}, ctx
            )
        )["status"] == "error"

    asyncio.run(run())


# ---- edit: replace/append transfers ----


def test_edit_append_and_replace_update_row_and_announce_upsert(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        emitted = []
        ctx = _ctx(emitted, turn_id=5)
        document_id = await _create(ctx, content="v1")

        result = await _tool("edit_document").handler(
            {"document_id": document_id, "mode": "append", "content": "v2"}, ctx
        )
        assert result["status"] == "edited"
        result = await _tool("edit_document").handler(
            {
                "document_id": document_id,
                "mode": "replace",
                "content": "clean",
                "title": "Renamed",
            },
            ctx,
        )
        assert result["status"] == "edited"

        async with session_factory()() as db:
            row = (await db.execute(select(Document))).scalars().one()
        assert (row.content, row.title) == ("clean", "Renamed")
        assert row.updated_at is not None

        updates = [e for e in emitted if e["type"] == "document.updated"]
        assert updates[0]["document"]["content"] == "v1\nv2"  # post-edit payload
        assert updates[1]["document"]["title"] == "Renamed"

    asyncio.run(run())


def test_replace_refused_past_read_cap_append_still_works(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        big_content = "x" * (READ_DOCUMENT_MAX_CHARS + 1)
        row = await insert_upload_row("uid-a", "big.txt", "text", big_content)
        result = await _tool("edit_document").handler(
            {"document_id": row.id, "mode": "replace", "content": "tiny"}, ctx
        )
        assert result["status"] == "refused"
        assert "str_replace or append" in result["error"]
        result = await _tool("edit_document").handler(
            {"document_id": row.id, "mode": "append", "content": "tail"}, ctx
        )
        assert result["status"] == "edited"

    asyncio.run(run())


def test_concurrent_appends_lose_neither(tmp_path):
    """Mandated (i): the atomic DB-side concatenation — two appends racing
    (as when a write outlives its turn, FR-46) both land."""

    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        document_id = await _create(ctx, content="base")
        edit = _tool("edit_document").handler
        await asyncio.gather(
            edit({"document_id": document_id, "mode": "append", "content": "left"}, ctx),
            edit({"document_id": document_id, "mode": "append", "content": "right"}, ctx),
        )
        async with session_factory()() as db:
            content = (await db.execute(select(Document.content))).scalar_one()
        assert "left" in content and "right" in content and content.startswith("base")

    asyncio.run(run())


def test_one_create_n_edits_aggregates_one_count_n_edits(tmp_path):
    """Mandated (ii): never N+1 documents."""

    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        document_id = await _create(ctx)
        for i in range(3):
            await _tool("edit_document").handler(
                {"document_id": document_id, "mode": "append", "content": f"e{i}"},
                ctx,
            )
        async with session_factory()() as db:
            docs = (await db.execute(select(Document))).scalars().all()
            events = (
                (
                    await db.execute(
                        select(UsageEvent).where(UsageEvent.stage == "artifact")
                    )
                )
                .scalars()
                .all()
            )
        assert len(docs) == 1
        units = sorted(e.unit for e in events)
        assert units == ["count", "edits", "edits", "edits"]

    asyncio.run(run())


# ---- NFR-8 negative ----


def test_another_users_document_reads_as_not_found(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        theirs = await _create(_ctx([], user_id="uid-b", session_id="s-b"))
        mine = _ctx([], user_id="uid-a", session_id="s-a")

        got = await _tool("read_document").handler({"document_id": theirs}, mine)
        assert got["status"] == "not_found"
        listing = await _tool("list_documents").handler({}, mine)
        assert theirs not in [d["id"] for d in listing["documents"]]
        for args in (
            {"mode": "replace", "content": "hijack"},
            {"mode": "append", "content": "hijack"},
            {"mode": "str_replace", "old_str": "body", "new_str": "hijack"},
        ):
            got = await _tool("edit_document").handler(
                {"document_id": theirs, **args}, mine
            )
            assert got["status"] == "not_found"
        # and the row is untouched
        async with session_factory()() as db:
            content = (await db.execute(select(Document.content))).scalar_one()
        assert "hijack" not in content

    asyncio.run(run())


# ---- failure discipline ----


def test_bad_arguments_return_error_results(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        edit = _tool("edit_document").handler
        assert (await edit({"document_id": "x", "mode": "append", "content": "c"}, ctx))["status"] == "error"
        assert (await edit({"document_id": 1, "mode": "rewrite", "content": "c"}, ctx))["status"] == "error"
        assert (await edit({"document_id": 1, "mode": "append", "content": " "}, ctx))["status"] == "error"
        create = _tool("create_document").handler
        assert (await create({"title": "T", "kind": "summary", "content": ""}, ctx))["status"] == "error"

    asyncio.run(run())


def test_db_failure_returns_error_result_without_raising(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(tools, "create_document_row", broken)
    monkeypatch.setattr(tools, "append_document_content", broken)
    monkeypatch.setattr(tools, "list_workspace_rows", broken)
    monkeypatch.setattr(tools, "read_document_page", broken)
    monkeypatch.setattr(tools, "search_workspace", broken)

    async def run():
        emitted = []
        ctx = _ctx(emitted)
        create = await _tool("create_document").handler(
            {"title": "T", "kind": "summary", "content": "body"}, ctx
        )
        assert create["status"] == "error"
        edit = await _tool("edit_document").handler(
            {"document_id": 1, "mode": "append", "content": "c"}, ctx
        )
        assert edit["status"] == "error"
        listing = await _tool("list_documents").handler({}, ctx)
        assert listing["status"] == "error"
        got = await _tool("read_document").handler({"document_id": 1}, ctx)
        assert got["status"] == "error"
        search = await _tool("search_documents").handler({"query": "x"}, ctx)
        assert search["status"] == "error"
        assert emitted == []  # no announce for a failed write

    asyncio.run(run())


def test_unknown_kind_is_coerced_to_summary(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        await _tool("create_document").handler(
            {"title": "T", "kind": "haiku", "content": "body"}, ctx
        )
        async with session_factory()() as db:
            row = (await db.execute(select(Document))).scalars().one()
        assert row.kind == "summary"

    asyncio.run(run())


def test_edit_event_files_under_the_editing_session(tmp_path):
    """The tool's defining case edits a PRIOR-session document — the
    artifact_edited event must carry the EDITING session (whose turn number
    it holds), or the cost vanishes from the current drill-down."""

    async def run():
        await _setup_db(tmp_path)
        await create_session_row("s-a2", "uid-a")  # today's session
        created_in = _ctx([], session_id="s-a")  # yesterday's
        document_id = await _create(created_in, title="Old plan")

        editing = _ctx([], session_id="s-a2", turn_id=9)
        result = await _tool("edit_document").handler(
            {"document_id": document_id, "mode": "append", "content": "new bullet"},
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


def test_replace_race_where_row_grows_after_the_read_returns_refused(
    tmp_path, monkeypatch
):
    """The pre-read saw a SMALL document (stale), the row grew past the cap
    before the UPDATE — the result must be the refusal steering the model
    away from replace, and the content must survive (the cap is enforced in
    the UPDATE's own predicate)."""
    from types import SimpleNamespace

    async def run():
        await _setup_db(tmp_path)
        ctx = _ctx([])
        big_content = "x" * (READ_DOCUMENT_MAX_CHARS + 1)
        row = await insert_upload_row("uid-a", "big.txt", "text", big_content)

        real_get = tools.get_document_row
        calls = {"n": 0}

        async def stale_first_read(user_id, document_id):
            calls["n"] += 1
            if calls["n"] == 1:  # the read that raced the detached write
                return SimpleNamespace(content="small")
            return await real_get(user_id, document_id)

        monkeypatch.setattr(tools, "get_document_row", stale_first_read)
        result = await _tool("edit_document").handler(
            {"document_id": row.id, "mode": "replace", "content": "tiny"}, ctx
        )
        assert result["status"] == "refused"
        async with session_factory()() as db:
            content = (await db.execute(select(Document.content))).scalar_one()
        assert "tiny" not in content

    asyncio.run(run())
