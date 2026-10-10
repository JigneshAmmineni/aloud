"""FR-50: the run-once artifacts→documents migration (RLS-bootstrap slot).

Runs pre-serve on the bootstrap engine — `bootstrap_session()` raises rather
than silently seeing zero rows, which is the identity precondition: the copy
must run RLS-exempt, and a wrong identity here would "succeed" on zero rows
right before the drop release destroys the originals. Postgres-only, like
the rest of the RLS bootstrap (the SQLite suite never carries legacy rows;
the migration's mandated tests run in the Postgres-only lane).

Run-once means a persisted marker with a watermark (`schema_migrations`,
key `artifacts_to_documents`, max migrated artifacts.id), never a data
heuristic: insert-where-absent re-run on every boot would resurrect deleted
documents, and "is the table empty" breaks the day uploads share it. Later
boots copy only rows ABOVE the watermark (normally none; after a rollback,
exactly the rollback-window writes) and refresh rollback-window EDITS to
already-migrated rows with a last-writer-wins rule compared as
COALESCE(updated_at, created_at) on BOTH sides — a bare updated_at
comparison would drop edits to never-edited rows.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import text

from db.engine import bootstrap_session

MIGRATION_KEY = "artifacts_to_documents"


async def _source_count(db, watermark: int) -> int:
    """Rows the copy is about to move. A separate function so the mandated
    short-circuit test can force a mismatch without touching real data."""
    return (
        await db.execute(
            text("SELECT count(*) FROM artifacts WHERE id > :wm"),
            {"wm": watermark},
        )
    ).scalar_one()


async def migrate_artifacts_to_documents() -> None:
    """Copy every artifacts row above the watermark into documents, refresh
    rollback-window edits, then write the marker — one transaction, so a
    short-circuited copy leaves no marker and no watermark (FR-50)."""
    async with bootstrap_session() as db:
        if db.bind.dialect.name != "postgresql":
            return  # the slot is Postgres-only, like _bootstrap_rls

        marker = (
            await db.execute(
                text("SELECT watermark FROM schema_migrations WHERE key = :k"),
                {"k": MIGRATION_KEY},
            )
        ).first()
        watermark = marker[0] if marker and marker[0] is not None else -1

        expected = await _source_count(db, watermark)
        copied = (
            await db.execute(
                text(
                    """
                    INSERT INTO documents
                        (user_id, session_id, source, format, kind,
                         legacy_artifact_id, created_at, updated_at,
                         title, content)
                    SELECT user_id, session_id, 'agent', 'markdown', kind,
                           id, created_at, updated_at, title, content
                    FROM artifacts WHERE id > :wm
                    """
                ),
                {"wm": watermark},
            )
        ).rowcount

        if copied != expected:
            # Counts and ids only — never title/content (NFR-9). The raise
            # rolls the transaction back (no marker, no watermark) and fails
            # boot: the deploy's health check must not pass over lost rows.
            logger.bind(
                component="db",
                event="migration.short_circuit",
                key=MIGRATION_KEY,
                expected=expected,
                copied=copied,
                watermark=watermark,
            ).error("artifacts→documents copy short-circuited; rolling back")
            raise RuntimeError(
                f"migration {MIGRATION_KEY}: copied {copied} of {expected} rows"
            )

        # Rollback-window edits to already-migrated rows: last-writer-wins,
        # COALESCE on both sides (a never-edited row has NULL updated_at).
        refreshed = (
            await db.execute(
                text(
                    """
                    UPDATE documents d
                    SET title = a.title, content = a.content,
                        updated_at = a.updated_at, kind = a.kind
                    FROM artifacts a
                    WHERE d.legacy_artifact_id = a.id
                      AND COALESCE(a.updated_at, a.created_at)
                          > COALESCE(d.updated_at, d.created_at)
                    """
                )
            )
        ).rowcount

        new_watermark = (
            await db.execute(text("SELECT max(id) FROM artifacts"))
        ).scalar()
        if new_watermark is None and watermark >= 0:
            new_watermark = watermark  # source emptied post-drop prep: keep it
        await db.execute(
            text(
                """
                INSERT INTO schema_migrations (key, completed_at, watermark)
                VALUES (:k, :ts, :wm)
                ON CONFLICT (key) DO UPDATE
                SET completed_at = EXCLUDED.completed_at,
                    watermark = EXCLUDED.watermark
                """
            ),
            {
                "k": MIGRATION_KEY,
                "ts": datetime.now(timezone.utc),
                "wm": new_watermark,
            },
        )
        await db.commit()
        logger.bind(
            component="db",
            event="migration.completed",
            key=MIGRATION_KEY,
            copied=copied,
            refreshed=refreshed,
            watermark=new_watermark,
        ).info(
            f"artifacts→documents: {copied} copied, {refreshed} refreshed"
        )
