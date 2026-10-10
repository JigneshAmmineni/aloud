"""FR-50: the artifacts→documents migration — Postgres-only lane.

The migration lives in the RLS-bootstrap slot (Postgres-only), so its
mandated tests run here, gated like test_rls.py. The target DB persists
between runs (CI: throwaway service; locally: the compose db), so every
test seeds unique rows and asserts only about them — the watermark/marker
are global state these tests advance, never assume absent.
"""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, text, update

import db.migrations as migrations
from db.engine import bootstrap_session, init_db, user_scoped_session
from db.migrations import MIGRATION_KEY, migrate_artifacts_to_documents
from db.models import Artifact, Document
from db.sessions_repo import create_session_row
from db.users_repo import provision_user

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="RLS_TEST_DATABASE_URL not set (Postgres required)"
)


async def _seed_artifact(uid: str, sess: str, title="t", content="c", kind="summary"):
    async with user_scoped_session(uid) as db:
        row = Artifact(
            session_id=sess, user_id=uid, kind=kind, title=title, content=content
        )
        db.add(row)
        await db.commit()
        return row.id


async def _marker():
    async with bootstrap_session() as db:
        return (
            await db.execute(
                text("SELECT watermark FROM schema_migrations WHERE key = :k"),
                {"k": MIGRATION_KEY},
            )
        ).first()


async def _doc_for(uid: str, legacy_id: int) -> Document | None:
    async with user_scoped_session(uid) as db:
        return (
            await db.execute(
                select(Document).where(Document.legacy_artifact_id == legacy_id)
            )
        ).scalar_one_or_none()


def test_migration_copies_every_field_and_advances_watermark():
    """(a) a seeded legacy artifacts row surfaces post-migration as an agent
    document, all fields intact — kind and session_id included (a NULL kind
    renders FR-54's badge blank and writes NULL detail usage events)."""
    uid, sess = f"mig-a-{uuid.uuid4()}", f"s-{uuid.uuid4()}"

    async def run():
        await init_db(PG_URL)
        await provision_user(uid, None)
        await create_session_row(sess, uid)
        art_id = await _seed_artifact(
            uid, sess, title="notes", content="body", kind="action_items"
        )
        await migrate_artifacts_to_documents()

        doc = await _doc_for(uid, art_id)
        assert doc is not None
        assert doc.source == "agent"
        assert doc.format == "markdown"
        assert doc.kind == "action_items"
        assert doc.session_id == sess
        assert doc.title == "notes"
        assert doc.content == "body"
        marker = await _marker()
        assert marker is not None and marker[0] >= art_id

    asyncio.run(run())


def test_migration_runs_once_no_resurrection_and_uploads_copy_nothing():
    """(b) delete a migrated document, boot again → the row STAYS deleted;
    an upload sharing the table is never duplicated by a re-run."""
    uid, sess = f"mig-b-{uuid.uuid4()}", f"s-{uuid.uuid4()}"

    async def run():
        await init_db(PG_URL)
        await provision_user(uid, None)
        await create_session_row(sess, uid)
        art_id = await _seed_artifact(uid, sess)
        await migrate_artifacts_to_documents()
        assert await _doc_for(uid, art_id) is not None

        # the user hard-deletes the migrated document (FR-52 semantics)
        async with user_scoped_session(uid) as db:
            await db.execute(
                delete(Document).where(Document.legacy_artifact_id == art_id)
            )
            # an upload lands in the shared table
            db.add(
                Document(
                    user_id=uid, source="uploaded", format="text",
                    title="up.txt", content="u",
                )
            )
            await db.commit()

        await migrate_artifacts_to_documents()  # next boot

        assert await _doc_for(uid, art_id) is None  # no resurrection
        async with user_scoped_session(uid) as db:
            uploads = (
                (
                    await db.execute(
                        select(Document).where(
                            Document.user_id == uid,
                            Document.source == "uploaded",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(uploads) == 1  # copied nothing, duplicated nothing

    asyncio.run(run())


def test_short_circuit_writes_no_marker_and_no_rows(monkeypatch):
    """A copy that yields fewer rows than the source rolls everything back:
    no marker advance, no copied rows, boot fails loudly."""
    uid, sess = f"mig-c-{uuid.uuid4()}", f"s-{uuid.uuid4()}"

    async def run():
        await init_db(PG_URL)
        await provision_user(uid, None)
        await create_session_row(sess, uid)
        before = await _marker()
        art_id = await _seed_artifact(uid, sess)

        real_count = migrations._source_count

        async def inflated(db, wm):
            return await real_count(db, wm) + 1

        monkeypatch.setattr(migrations, "_source_count", inflated)
        with pytest.raises(RuntimeError):
            await migrate_artifacts_to_documents()

        assert await _doc_for(uid, art_id) is None  # copy rolled back
        after = await _marker()
        assert after == before  # no marker, no watermark advance

    asyncio.run(run())


def test_rollforward_picks_up_window_writes_while_deletes_stay_deleted():
    """Rows written to artifacts below a rolled-back release are picked up
    on roll-forward (the watermark advance) while a deleted migrated
    document still stays deleted across that same re-run; rollback-window
    EDITS refresh by the COALESCE last-writer-wins rule."""
    uid, sess = f"mig-d-{uuid.uuid4()}", f"s-{uuid.uuid4()}"

    async def run():
        await init_db(PG_URL)
        await provision_user(uid, None)
        await create_session_row(sess, uid)
        kept_id = await _seed_artifact(uid, sess, content="v1")
        doomed_id = await _seed_artifact(uid, sess)
        await migrate_artifacts_to_documents()

        # rollback window: the user deletes one migrated document...
        async with user_scoped_session(uid) as db:
            await db.execute(
                delete(Document).where(Document.legacy_artifact_id == doomed_id)
            )
            await db.commit()
        # ...old code edits an already-migrated artifact...
        async with user_scoped_session(uid) as db:
            await db.execute(
                update(Artifact)
                .where(Artifact.id == kept_id)
                .values(
                    content="v2-rollback-edit",
                    updated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                )
            )
            await db.commit()
        # ...and old code writes a brand-new artifact (id above watermark)
        new_id = await _seed_artifact(uid, sess, title="window-write")

        await migrate_artifacts_to_documents()  # roll-forward boot

        assert (await _doc_for(uid, new_id)).title == "window-write"
        assert (await _doc_for(uid, kept_id)).content == "v2-rollback-edit"
        assert await _doc_for(uid, doomed_id) is None  # still deleted

    asyncio.run(run())


def test_documents_nfr8_negative_under_real_rls():
    """User A cannot read user B's documents rows — no WHERE needed, the
    policy filters (the NFR-8 negative FR-50 mandates on documents)."""
    uid_a, uid_b = f"mig-e-{uuid.uuid4()}", f"mig-f-{uuid.uuid4()}"

    async def run():
        await init_db(PG_URL)
        await provision_user(uid_a, None)
        await provision_user(uid_b, None)
        async with user_scoped_session(uid_b) as db:
            db.add(
                Document(
                    user_id=uid_b, source="uploaded", format="text",
                    title="b.txt", content="secret",
                )
            )
            await db.commit()

        async with user_scoped_session(uid_a) as db:
            rows = (await db.execute(select(Document))).scalars().all()
            assert uid_b not in {r.user_id for r in rows}

    asyncio.run(run())
