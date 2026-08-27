"""Provisioning upsert contracts (FR-24): atomic, fill-only, order-proof."""

import asyncio

from db.engine import init_db, session_factory
from db.models import User
from db.users_repo import provision_user


def _run(tmp_path, coro_fn):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/repo_test.db")
        await coro_fn()

    asyncio.run(run())


async def _name_of(uid: str):
    async with session_factory()() as db:
        row = await db.get(User, uid)
        return row.preferred_name if row else None


def test_named_signup_then_nameless_provision_keeps_name(tmp_path):
    async def scenario():
        await provision_user("u1", "Ada")
        await provision_user("u1", None)  # e.g. a later /start
        assert await _name_of("u1") == "Ada"

    _run(tmp_path, scenario)


def test_nameless_provision_then_named_call_backfills(tmp_path):
    async def scenario():
        await provision_user("u2", None)
        await provision_user("u2", "Grace")  # fill-only backfill
        assert await _name_of("u2") == "Grace"

    _run(tmp_path, scenario)


def test_existing_name_is_never_overwritten(tmp_path):
    async def scenario():
        await provision_user("u3", "Ada")
        await provision_user("u3", "Impostor")
        assert await _name_of("u3") == "Ada"

    _run(tmp_path, scenario)


def test_name_is_trimmed_and_length_capped(tmp_path):
    async def scenario():
        await provision_user("u4", "  " + "x" * 200 + "  ")
        name = await _name_of("u4")
        assert name == "x" * 80  # FR-30 cap

        await provision_user("u5", "   ")
        assert await _name_of("u5") is None  # whitespace-only -> no name

    _run(tmp_path, scenario)
