"""Admin query family (FR-35/36/37) on sqlite — aggregate correctness and
the NFR-9 serialization contract. RLS-proper admin tests live in test_rls.py
(Postgres); here admin_scoped_session's dialect guard skips the SET calls."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.auth import AuthedUser
from db import admin_repo
from db.engine import init_db, user_scoped_session
from db.models import Session, TranscriptEvent, TurnMetric, UsageEvent
from db.users_repo import provision_user

ADMIN = AuthedUser(
    user_id="admin-x",
    email="admin@example.com",
    email_verified=True,
    name=None,
    is_admin=True,
)

NOW = datetime.now(timezone.utc)
PRIVATE_TEXT = "the user's private words"


async def _seed(tmp_path):
    """User A: an old ended session rich in per-turn data + a recent one.
    User B: a recent session that ended in error."""
    await init_db(f"sqlite+aiosqlite:///{tmp_path}/admin_test.db")
    await provision_user("uid-a", None)
    await provision_user("uid-b", None)

    async with user_scoped_session("uid-a") as db:
        db.add(
            Session(
                id="sa1", user_id="uid-a", status="ended", end_reason="user",
                started_at=NOW - timedelta(days=3),
                ended_at=NOW - timedelta(days=3) + timedelta(seconds=120),
            )
        )
        db.add(
            Session(
                id="sa2", user_id="uid-a", status="ended", end_reason="user",
                started_at=NOW - timedelta(hours=2),
                ended_at=NOW - timedelta(hours=2) + timedelta(seconds=60),
            )
        )
        ts = NOW - timedelta(days=3)
        for turn, stage, unit, qty, detail in (
            (1, "llm", "tokens_in", 100.0, None),
            (1, "llm", "tokens_out", 50.0, None),
            (1, "tts", "characters", 200.0, None),
            (2, "llm", "tokens_in", 80.0, None),  # barged-in: cost, no latency
            (None, "stt", "seconds", 120.0, None),
            (None, "artifact", "count", 1.0, "summary"),
        ):
            db.add(
                UsageEvent(
                    user_id="uid-a", session_id="sa1", turn_id=turn, ts=ts,
                    stage=stage, unit=unit, quantity=qty, detail=detail,
                )
            )
        db.add(
            TurnMetric(
                user_id="uid-a", session_id="sa1", turn_id=1, ts=ts,
                eot_to_first_audio_ms=1200, stages_ms={"ttfb.llm": 700},
            )
        )
        # turn 3: latency but zero usage — must still appear in the union
        db.add(
            TurnMetric(
                user_id="uid-a", session_id="sa1", turn_id=3, ts=ts,
                eot_to_first_audio_ms=3500, stages_ms=None,
            )
        )
        db.add(
            TranscriptEvent(
                session_id="sa1", user_id="uid-a", ts=ts, role="user",
                kind="final_transcript", text=PRIVATE_TEXT,
            )
        )
        await db.commit()

    async with user_scoped_session("uid-b") as db:
        db.add(
            Session(
                id="sb1", user_id="uid-b", status="ended", end_reason="error",
                started_at=NOW - timedelta(hours=1),
                ended_at=NOW - timedelta(minutes=50),
            )
        )
        db.add(
            UsageEvent(
                user_id="uid-b", session_id="sb1", turn_id=None,
                ts=NOW - timedelta(minutes=50), stage="stt", unit="seconds",
                quantity=600.0,
            )
        )
        db.add(
            TurnMetric(
                user_id="uid-b", session_id="sb1", turn_id=1,
                ts=NOW - timedelta(minutes=55), eot_to_first_audio_ms=900,
                stages_ms=None,
            )
        )
        await db.commit()


def test_user_aggregates(tmp_path):
    async def run():
        await _seed(tmp_path)
        agg = await admin_repo.user_aggregates(ADMIN)
        a, b = agg["uid-a"], agg["uid-b"]
        assert a["sessions"] == 2
        assert a["usage"]["llm.tokens_in"] == 180.0
        assert a["usage"]["llm.tokens_out"] == 50.0
        assert a["usage"]["tts.characters"] == 200.0
        assert a["usage"]["stt.seconds"] == 120.0
        assert a["usage"]["artifact.count"] == 1.0
        assert b["sessions"] == 1
        assert b["usage"]["stt.seconds"] == 600.0
        # last_active = most recent session start: sb1 (1h ago) is more
        # recent than sa2 (2h ago); ISO strings compare chronologically
        assert b["last_active"] > a["last_active"]

    asyncio.run(run())


def test_sessions_for_user(tmp_path):
    async def run():
        await _seed(tmp_path)
        sessions = await admin_repo.sessions_for_user(ADMIN, "uid-a")
        assert [s["session_id"] for s in sessions] == ["sa2", "sa1"]  # newest first
        sa1 = sessions[1]
        assert sa1["duration_s"] == 120.0
        assert sa1["end_reason"] == "user"
        assert sa1["artifact_count"] == 1  # from the artifact.count event
        assert sa1["median_turn_ms"] == 1200
        assert sa1["worst_turn_ms"] == 3500
        assert sa1["usage"]["llm.tokens_in"] == 180.0

    asyncio.run(run())


def test_session_detail_union_of_latency_and_usage(tmp_path):
    """FR-33's barge-in mandate: the per-turn table is the UNION — turn 2
    has cost but no latency row; turn 3 has latency but no cost."""

    async def run():
        await _seed(tmp_path)
        detail = await admin_repo.session_detail(ADMIN, "sa1")
        turns = {t["turn_id"]: t for t in detail["turns"]}
        assert sorted(turns) == [1, 2, 3]
        assert turns[1]["eot_to_first_audio_ms"] == 1200
        assert turns[1]["usage"]["llm.tokens_in"] == 100.0
        assert turns[2]["eot_to_first_audio_ms"] is None  # barged-in, spent anyway
        assert turns[2]["usage"]["llm.tokens_in"] == 80.0
        assert turns[3]["eot_to_first_audio_ms"] == 3500
        assert turns[3]["usage"] == {}
        # session totals include the turn-less stt + artifact events
        assert detail["usage"]["stt.seconds"] == 120.0
        assert detail["usage"]["artifact.count"] == 1.0
        assert await admin_repo.session_detail(ADMIN, "nope") is None

    asyncio.run(run())


def test_admin_responses_never_serialize_content(tmp_path):
    """FR-38 (d) serialization half / NFR-9: no admin payload carries
    transcript text, artifact content, or keys that could."""

    def assert_clean(payload):
        dumped = json.dumps(payload, default=str)
        assert PRIVATE_TEXT not in dumped

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    assert k not in ("text", "content", "title")
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(payload)

    async def run():
        await _seed(tmp_path)
        assert_clean(await admin_repo.user_aggregates(ADMIN))
        assert_clean(await admin_repo.sessions_for_user(ADMIN, "uid-a"))
        assert_clean(await admin_repo.session_detail(ADMIN, "sa1"))
        assert_clean(await admin_repo.overview(ADMIN))

    asyncio.run(run())


def test_overview_aggregates(tmp_path):
    async def run():
        await _seed(tmp_path)
        data = await admin_repo.overview(ADMIN)
        # today (24h): sa2 + sb1; 7d additionally sa1
        assert data["today"] == {"sessions": 2, "unique_users": 2}
        assert data["last_7d"] == {"sessions": 3, "unique_users": 2}
        assert data["usage_7d"]["stt.seconds"] == 720.0
        assert data["usage_30d"]["llm.tokens_in"] == 180.0
        # 24h latency: only sb1's 900ms metric is recent
        assert data["turn_latency_24h"]["turns"] == 1
        assert data["turn_latency_24h"]["p50_ms"] == 900
        assert data["turn_latency_24h"]["nfr1_breaches"] == 0
        assert data["error_sessions_24h"] == 1  # sb1

    asyncio.run(run())


def test_admin_scoped_session_requires_admin():
    """FR-38: the gate is structural — a non-admin AuthedUser (or a plain
    string) is a loud PermissionError before any SQL runs."""
    not_admin = AuthedUser(
        user_id="u", email=None, email_verified=True, name=None, is_admin=False
    )

    async def run():
        with pytest.raises(PermissionError):
            async with admin_repo.admin_scoped_session(not_admin):
                pass
        with pytest.raises(PermissionError):
            async with admin_repo.admin_scoped_session("uid-as-string"):  # type: ignore[arg-type]
                pass

    asyncio.run(run())
