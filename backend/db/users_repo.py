"""User provisioning (FR-24): atomic, fill-only upsert keyed on uid.

Called from /start. COALESCE semantics: a provided name fills a missing one;
an absent name never overwrites a stored one — so neither request ordering
(nameless provision first, or named signup first) can drop or null the name.
"""

from sqlalchemy import func

from db.engine import session_factory
from db.models import User

NAME_MAX_LEN = 80  # FR-30: length-capped untrusted input


def _insert_for(dialect: str):
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


async def provision_user(user_id: str, preferred_name: str | None) -> None:
    name = (preferred_name or "").strip()[:NAME_MAX_LEN] or None
    async with session_factory()() as db:
        insert = _insert_for(db.bind.dialect.name)
        stmt = insert(User).values(id=user_id, preferred_name=name)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "preferred_name": func.coalesce(
                    User.preferred_name, stmt.excluded.preferred_name
                )
            },
        )
        await db.execute(stmt)
        await db.commit()
