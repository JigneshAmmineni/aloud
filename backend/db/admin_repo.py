"""Admin cross-user reads (FR-35/36/37) through the FR-38 RLS escape.

This is a DISTINCT query family from the user-scoped repos: these functions
take the admin's AuthedUser (structurally — CLAUDE.md's user_id-no-default
rule applies to the user-scoped family, not here) and read across users via
`admin_scoped_session`, which:
  - raises unless the caller's AuthedUser has is_admin (a forgotten FR-28
    gate is a loud error, not a silent cross-user read),
  - sets the transaction READ ONLY (an accidental write fails at the DB),
  - sets `app.is_admin` transaction-locally (like app.user_id, it can never
    leak across a pooled connection).
The escape only widens FOR SELECT on sessions/usage_events/turn_metrics —
content tables return zero rows even here (NFR-9's DB backstop).
"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.auth import AuthedUser
from db.engine import session_factory
from db.models import Session, TurnMetric, UsageEvent


@asynccontextmanager
async def admin_scoped_session(admin: AuthedUser):
    if not isinstance(admin, AuthedUser) or not admin.is_admin:
        raise PermissionError("admin_scoped_session requires an admin AuthedUser")
    async with session_factory()() as db:
        if db.bind.dialect.name == "postgresql":
            await db.execute(text("SET TRANSACTION READ ONLY"))
            await db.execute(select(func.set_config("app.is_admin", "true", True)))
        yield db


def _percentile(sorted_values: list, q: float):
    """Nearest-rank percentile in Python — portable across Postgres/SQLite
    (percentile_cont is Postgres-only), fine at current row counts."""
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]


async def user_aggregates(admin: AuthedUser) -> dict[str, dict]:
    """Per-user rollups for the admin list (FR-35): sessions, usage units,
    last-active. Keyed by user_id; merged with Firebase accounts in the route."""
    out: dict[str, dict] = {}
    async with admin_scoped_session(admin) as db:
        session_rows = await db.execute(
            select(
                Session.user_id,
                func.count(Session.id),
                func.max(Session.started_at),
            ).group_by(Session.user_id)
        )
        for user_id, count, last in session_rows:
            out[user_id] = {
                "sessions": count,
                "last_active": last.isoformat() if last else None,
                "usage": {},
            }
        usage_rows = await db.execute(
            select(
                UsageEvent.user_id,
                UsageEvent.stage,
                UsageEvent.unit,
                func.sum(UsageEvent.quantity),
            ).group_by(UsageEvent.user_id, UsageEvent.stage, UsageEvent.unit)
        )
        for user_id, stage, unit, total in usage_rows:
            entry = out.setdefault(
                user_id, {"sessions": 0, "last_active": None, "usage": {}}
            )
            entry["usage"][f"{stage}.{unit}"] = float(total or 0)
    return out


async def sessions_for_user(admin: AuthedUser, user_id: str) -> list[dict]:
    """FR-36: a user's session history with per-session usage and latency."""
    async with admin_scoped_session(admin) as db:
        sessions = (
            (
                await db.execute(
                    select(Session)
                    .where(Session.user_id == user_id)
                    .order_by(Session.started_at.desc())
                )
            )
            .scalars()
            .all()
        )
        usage_rows = await db.execute(
            select(
                UsageEvent.session_id,
                UsageEvent.stage,
                UsageEvent.unit,
                func.sum(UsageEvent.quantity),
            )
            .where(UsageEvent.user_id == user_id)
            .group_by(UsageEvent.session_id, UsageEvent.stage, UsageEvent.unit)
        )
        usage: dict[str, dict] = {}
        for sid, stage, unit, total in usage_rows:
            usage.setdefault(sid, {})[f"{stage}.{unit}"] = float(total or 0)
        latency_rows = await db.execute(
            select(TurnMetric.session_id, TurnMetric.eot_to_first_audio_ms).where(
                TurnMetric.user_id == user_id
            )
        )
        latencies: dict[str, list] = {}
        for sid, ms in latency_rows:
            latencies.setdefault(sid, []).append(ms)

    result = []
    for s in sessions:
        lat = sorted(latencies.get(s.id, []))
        duration_s = (
            (s.ended_at - s.started_at).total_seconds()
            if s.ended_at is not None
            else None
        )
        result.append(
            {
                "session_id": s.id,
                "started_at": s.started_at.isoformat(),
                "duration_s": duration_s,
                "status": s.status,
                "end_reason": s.end_reason,
                "usage": usage.get(s.id, {}),
                "artifact_count": int(usage.get(s.id, {}).get("artifact.count", 0)),
                "median_turn_ms": _percentile(lat, 0.5),
                "worst_turn_ms": lat[-1] if lat else None,
            }
        )
    return result


async def session_detail(admin: AuthedUser, session_id: str) -> dict | None:
    """FR-36 drill-down: the per-turn table joining latency with that turn's
    usage — driven from the union of both tables (a barged-in turn may have
    cost but no latency row), never an inner join from latency."""
    async with admin_scoped_session(admin) as db:
        s = (
            await db.execute(select(Session).where(Session.id == session_id))
        ).scalar()
        if s is None:
            return None
        metric_rows = (
            (
                await db.execute(
                    select(TurnMetric)
                    .where(TurnMetric.session_id == session_id)
                    .order_by(TurnMetric.turn_id)
                )
            )
            .scalars()
            .all()
        )
        usage_rows = await db.execute(
            select(
                UsageEvent.turn_id,
                UsageEvent.stage,
                UsageEvent.unit,
                func.sum(UsageEvent.quantity),
            )
            .where(UsageEvent.session_id == session_id)
            .group_by(UsageEvent.turn_id, UsageEvent.stage, UsageEvent.unit)
        )

    turn_usage: dict[int | None, dict] = {}
    for turn_id, stage, unit, total in usage_rows:
        turn_usage.setdefault(turn_id, {})[f"{stage}.{unit}"] = float(total or 0)
    metrics_by_turn = {m.turn_id: m for m in metric_rows}
    turn_ids = sorted(
        set(metrics_by_turn) | {t for t in turn_usage if t is not None}
    )
    turns = [
        {
            "turn_id": t,
            "eot_to_first_audio_ms": (
                metrics_by_turn[t].eot_to_first_audio_ms
                if t in metrics_by_turn
                else None
            ),
            "stages_ms": (
                metrics_by_turn[t].stages_ms if t in metrics_by_turn else None
            ),
            "usage": turn_usage.get(t, {}),
        }
        for t in turn_ids
    ]
    session_usage: dict[str, float] = {}
    for units in turn_usage.values():
        for key, val in units.items():
            session_usage[key] = session_usage.get(key, 0.0) + val
    return {
        "session_id": s.id,
        "user_id": s.user_id,
        "started_at": s.started_at.isoformat(),
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "status": s.status,
        "end_reason": s.end_reason,
        "duration_s": (
            (s.ended_at - s.started_at).total_seconds() if s.ended_at else None
        ),
        "usage": session_usage,
        "turns": turns,
    }


async def overview(admin: AuthedUser) -> dict:
    """FR-37 aggregates (live-session count and cost labels added in the
    route): sessions/users today & 7d, usage by stage 7d/30d, latency
    p50/p95 (24h), NFR-1 breaches (24h), error-ended sessions (24h)."""
    now = datetime.now(timezone.utc)
    day, week, month = now - timedelta(days=1), now - timedelta(days=7), now - timedelta(days=30)
    async with admin_scoped_session(admin) as db:
        async def _sessions_since(since):
            row = (
                await db.execute(
                    select(
                        func.count(Session.id),
                        func.count(func.distinct(Session.user_id)),
                    ).where(Session.started_at >= since)
                )
            ).one()
            return {"sessions": row[0], "unique_users": row[1]}

        async def _usage_since(since):
            rows = await db.execute(
                select(
                    UsageEvent.stage,
                    UsageEvent.unit,
                    func.sum(UsageEvent.quantity),
                )
                .where(UsageEvent.ts >= since)
                .group_by(UsageEvent.stage, UsageEvent.unit)
            )
            return {f"{stage}.{unit}": float(total or 0) for stage, unit, total in rows}

        today = await _sessions_since(day)
        last7 = await _sessions_since(week)
        usage7 = await _usage_since(week)
        usage30 = await _usage_since(month)
        latencies = sorted(
            (
                await db.execute(
                    select(TurnMetric.eot_to_first_audio_ms).where(
                        TurnMetric.ts >= day
                    )
                )
            )
            .scalars()
            .all()
        )
        breaches = sum(1 for ms in latencies if ms > 3000)
        error_sessions = (
            await db.execute(
                select(func.count(Session.id)).where(
                    Session.started_at >= day, Session.end_reason == "error"
                )
            )
        ).scalar()
    return {
        "today": today,
        "last_7d": last7,
        "usage_7d": usage7,
        "usage_30d": usage30,
        "turn_latency_24h": {
            "p50_ms": _percentile(latencies, 0.5),
            "p95_ms": _percentile(latencies, 0.95),
            "turns": len(latencies),
            "nfr1_breaches": breaches,
        },
        "error_sessions_24h": int(error_sessions or 0),
    }
