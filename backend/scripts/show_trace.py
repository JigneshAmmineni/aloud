"""Pretty-print a session's LLM trace (FR-49) — the loop's exact execution
story: each step's input messages, streamed output, tool calls with
results, finish reason, tokens and timings.

Developer-grade BY DESIGN and deliberately behind DB access only, never a
product or admin surface: it reads any session's trace from a session_id
alone. It connects via DATABASE_URL — the compose user's superuser BYPASS
of row-level security, the one exemption FR-31 itself names (FORCE RLS is
on every table, so a merely-owner role would silently print an empty
session). If DATABASE_URL is ever hardened to a non-superuser role, this
script must switch to an explicit BYPASSRLS role or fail loudly — never
quietly show nothing (the guard below).

Usage (inside the backend container, grant_admin.py's pattern):

    docker compose run --rm backend python scripts/show_trace.py <session_id>
"""

import asyncio
import json
import os
import sys

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.models import LLMTrace  # noqa: E402


def _fmt_message(msg: dict) -> str:
    role = msg.get("role", "?").upper()
    content = msg.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, default=str)
    lines = [f"    [{role}] {content}"]
    for call in msg.get("tool_calls", []):
        lines.append(
            f"      -> tool_call {call.get('name')}"
            f"({json.dumps(call.get('arguments', {}), ensure_ascii=False)})"
        )
    return "\n".join(lines)


async def main(session_id: str) -> None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set (run inside the backend container).")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            if engine.dialect.name == "postgresql":
                # The superuser-bypass guard: FORCE RLS means a non-bypassing
                # role sees zero rows and this script would lie by silence.
                bypasses = (
                    await db.execute(
                        text(
                            "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                            "WHERE rolname = current_user"
                        )
                    )
                ).scalar()
                if not bypasses:
                    sys.exit(
                        "The connected role cannot bypass RLS — this script "
                        "would silently print an empty session. Connect with "
                        "a superuser or BYPASSRLS role (FR-49)."
                    )
            rows = (
                (
                    await db.execute(
                        select(LLMTrace)
                        .where(LLMTrace.session_id == session_id)
                        .order_by(LLMTrace.ts, LLMTrace.id)
                    )
                )
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()

    if not rows:
        print(f"No trace rows for session {session_id}.")
        return

    print(f"=== LLM trace: session {session_id} ({len(rows)} call(s)) ===")
    current_turn = object()
    for row in rows:
        if row.turn_id != current_turn:
            current_turn = row.turn_id
            print(f"\n--- turn {row.turn_id if row.turn_id is not None else '—'} ---")
        tokens = (
            f"{row.prompt_tokens}/{row.completion_tokens}"
            if row.prompt_tokens is not None
            else "—"
        )
        print(
            f"\n  step {row.step} [{row.purpose}] {row.model}  "
            f"finish={row.finish_reason}  tokens(in/out)={tokens}  "
            f"ttfb={row.ttfb_ms}ms  duration={row.duration_ms}ms  ({row.ts})"
        )
        print("  input:")
        try:
            for msg in json.loads(row.input_messages):
                print(_fmt_message(msg))
        except (ValueError, TypeError):
            print(f"    <unparseable input_messages: {row.input_messages[:200]}>")
        print("  output:")
        for line in (row.output or "<empty>").splitlines() or ["<empty>"]:
            print(f"    {line}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/show_trace.py <session_id>")
    asyncio.run(main(sys.argv[1]))
