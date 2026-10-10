"""db/documents_repo.py (§4.11 FR-51/52): quota, list ordering/continuation,
paired delete, and bounded attach resolution — the ordinary (SQLite) suite;
RLS-proper negatives live in the Postgres lane."""

import asyncio

import pytest
from sqlalchemy import select

import db.documents_repo as repo
from db.documents_repo import (
    DocumentQuotaError,
    create_document_row,
    delete_document_row,
    get_document_row,
    insert_upload_row,
    list_workspace_rows,
    resolve_attach_rows,
)
from db.engine import init_db, session_factory
from db.models import Artifact, Document, UsageEvent
from db.sessions_repo import create_session_row
from db.users_repo import provision_user


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path}/aloud_test.db"


def test_upload_insert_and_quota(db_url, monkeypatch):
    """Uploads persist with no usage event; the quota is one INSERT…SELECT
    statement whose refusal is the typed error, binding uploads and
    create_document alike (FR-51)."""
    monkeypatch.setattr(repo, "MAX_DOCUMENTS_PER_USER", 2)

    async def run():
        await init_db(db_url)
        await provision_user("uid-a", None)
        await provision_user("uid-b", None)
        await create_session_row("s-1", "uid-a")

        doc = await insert_upload_row("uid-a", "notes.md", "markdown", "hello")
        assert doc.id is not None and doc.source == "uploaded"
        await create_document_row("uid-a", "s-1", "summary", "t", "c", turn_id=1)

        with pytest.raises(DocumentQuotaError):
            await insert_upload_row("uid-a", "third.txt", "text", "x")
        with pytest.raises(DocumentQuotaError):
            await create_document_row("uid-a", "s-1", "summary", "t2", "c2", turn_id=2)

        # per-user: B is not bound by A's full quota
        await insert_upload_row("uid-b", "b.txt", "text", "y")

        async with session_factory()() as db:
            events = (await db.execute(select(UsageEvent))).scalars().all()
            # exactly one event: the create — uploads spend nothing
            assert len(events) == 1
            assert events[0].unit == "count" and events[0].detail == "summary"

    asyncio.run(run())


def test_workspace_list_ordering_continuation_and_no_content(db_url, monkeypatch):
    """Tiebroken ordering pages a QUIESCENT corpus with no repeats or skips
    (bulk-seeded rows tie on created_at, so id DESC is load-bearing);
    offset clamps at 0 and past-total returns empty with the same total."""
    monkeypatch.setattr(repo, "WORKSPACE_LIST_CAP", 4)

    async def run():
        await init_db(db_url)
        await provision_user("uid-a", None)
        async with session_factory()() as db:
            for i in range(10):  # one transaction: created_at ties everywhere
                db.add(
                    Document(
                        user_id="uid-a", source="uploaded", format="text",
                        title=f"d{i}", content="x" * (i + 1),
                    )
                )
            await db.commit()

        seen: list[int] = []
        offset = 0
        while True:
            items, total = await list_workspace_rows("uid-a", offset=offset)
            assert total == 10
            if not items:
                break
            seen += [it.id for it in items]
            offset += len(items)
        assert len(seen) == 10 and len(set(seen)) == 10  # no repeat, no skip
        assert seen == sorted(seen, reverse=True)  # id DESC under tied activity

        items, total = await list_workspace_rows("uid-a", offset=-5)  # clamps
        assert total == 10 and len(items) == 4
        assert items[0].char_count == 10  # computed length, content absent
        assert not hasattr(items[0], "content")

        items, total = await list_workspace_rows("uid-a", offset=99)
        assert items == [] and total == 10

    asyncio.run(run())


def test_delete_removes_paired_legacy_row_in_same_transaction(db_url):
    """FR-52 hard delete: the migrated document's retained artifacts row
    goes with it; usage events are untouched; a non-owner delete is a no-op
    returning False."""

    async def run():
        await init_db(db_url)
        await provision_user("uid-a", None)
        await provision_user("uid-b", None)
        await create_session_row("s-1", "uid-a")
        async with session_factory()() as db:
            legacy = Artifact(
                session_id="s-1", user_id="uid-a", kind="summary",
                title="t", content="c",
            )
            db.add(legacy)
            await db.flush()
            db.add(
                Document(
                    user_id="uid-a", session_id="s-1", source="agent",
                    format="markdown", kind="summary",
                    legacy_artifact_id=legacy.id, title="t", content="c",
                )
            )
            await db.commit()
            legacy_id = legacy.id

        docs, _ = await list_workspace_rows("uid-a")
        doc_id = docs[0].id

        assert await delete_document_row("uid-b", doc_id) is False  # not theirs
        assert await get_document_row("uid-a", doc_id) is not None

        assert await delete_document_row("uid-a", doc_id) is True
        assert await get_document_row("uid-a", doc_id) is None
        async with session_factory()() as db:
            assert await db.get(Artifact, legacy_id) is None  # paired delete

    asyncio.run(run())


def test_attach_resolution_is_budget_bounded(db_url):
    """FR-51: lengths-first walk in attach order; full fetch only for what
    fits, the boundary document sliced SQL-side, later ids skipped; unknown
    and unowned ids silently skipped."""

    async def run():
        await init_db(db_url)
        await provision_user("uid-a", None)
        await provision_user("uid-b", None)
        ids = []
        for i, content in enumerate(["a" * 10, "b" * 10, "c" * 10, "d" * 10]):
            row = await insert_upload_row("uid-a", f"f{i}.txt", "text", content)
            ids.append(row.id)
        other = await insert_upload_row("uid-b", "theirs.txt", "text", "z" * 10)

        docs = await resolve_attach_rows(
            "uid-a", [ids[0], 999999, other.id, ids[1], ids[2], ids[3]], budget=25
        )
        # 10 + 10 fit; the third slices to the remaining 5; the fourth skips
        assert [d.id for d in docs] == [ids[0], ids[1], ids[2]]
        assert docs[0].content == "a" * 10
        assert docs[2].content == "c" * 5  # substr slice, not truncate-after
        assert await resolve_attach_rows("uid-a", [], budget=100) == []

    asyncio.run(run())
