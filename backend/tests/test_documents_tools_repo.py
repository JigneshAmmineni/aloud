"""FR-53's repo half, ordinary (SQLite) lane: the in-process pager fold,
the portable str_replace/append predicates (counting, format gate, growth
ceiling), and the diagnostic's stated precedence. The dialect-bound pieces
(insert, search, multi-match line enumeration) live in test_documents_pg.py.
"""

import asyncio

import pytest
from sqlalchemy import select

from db.documents_repo import (
    EDITABLE_FORMATS,
    append_document_content,
    diagnose_edit_failure,
    fold_pages,
    insert_upload_row,
    page_index_for_line,
    read_document_page,
    render_page,
    str_replace_document,
)
from db.engine import init_db, session_factory
from db.models import Document, UsageEvent
from db.sessions_repo import create_session_row
from db.users_repo import provision_user

CEILING = 200  # small test ceiling, passed where tools pass MAX_DOC_CHARS


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite+aiosqlite:///{tmp_path}/aloud_test.db"


# --- the pure fold (test iv's rules, ordinary suite by design) -------------


def test_fold_snaps_page_boundaries_to_line_breaks():
    lines = ["a" * 20, "b" * 20, "c" * 20]
    pages = fold_pages("\n".join(lines), max_chars=50, max_lines=99)
    assert [[e.line_no for e in p] for p in pages] == [[1, 2], [3]]
    assert not any(e.continues for p in pages for e in p)


def test_fold_line_cap_bounds_decoration():
    pages = fold_pages("\n".join(["x"] * 7), max_chars=10_000, max_lines=3)
    assert [[e.line_no for e in p] for p in pages] == [[1, 2, 3], [4, 5, 6], [7]]


def test_fold_hard_cuts_long_line_and_keeps_its_number():
    pages = fold_pages("z" * 120, max_chars=50, max_lines=99)
    assert len(pages) == 3
    assert all(p[0].line_no == 1 for p in pages)  # the number survives the cut
    assert [p[0].continues for p in pages] == [True, True, False]
    assert render_page(pages[0]).endswith("[line continues]")
    assert "[line continues]" not in render_page(pages[2])


def test_fold_small_document_is_one_numbered_page():
    pages = fold_pages("alpha\nbeta", max_chars=8_000, max_lines=200)
    assert len(pages) == 1
    # line-numbered like any page — size-dependent decoration would make
    # insert's line addressing differ between small and large documents
    assert render_page(pages[0]) == "1: alpha\n2: beta"


def test_first_page_carrying_a_hard_cut_line():
    # line 1 fills page 1's window almost fully; line 2 snaps to page 2 and
    # hard-cuts across pages 2-4: its FIRST page is 2.
    content = "a" * 40 + "\n" + "b" * 120
    pages = fold_pages(content, max_chars=50, max_lines=99)
    assert page_index_for_line(pages, 2) == 1
    assert page_index_for_line(pages, 1) == 0
    assert page_index_for_line(pages, 99) is None


# --- read_document_page over the DB ----------------------------------------


def test_read_document_page_resolves_page_line_and_steers(db_url):
    async def run():
        await init_db(db_url)
        await provision_user("uid-a", None)
        row = await insert_upload_row(
            "uid-a", "notes.md", "markdown", "alpha\nbeta\ngamma"
        )
        page = await read_document_page("uid-a", row.id)
        assert page.page == 1 and page.total_pages == 1
        assert page.rendered == "1: alpha\n2: beta\n3: gamma"
        assert page.total_lines == 3

        by_line = await read_document_page("uid-a", row.id, line=2)
        assert by_line.page == 1

        assert await read_document_page("uid-a", row.id, page=5) == (
            "page_out_of_range:1"
        )
        assert await read_document_page("uid-a", row.id, line=99) == (
            "page_out_of_range:1"
        )
        assert await read_document_page("uid-b", row.id) == "not_found"

    asyncio.run(run())


# --- str_replace / append predicates ----------------------------------------


async def _seed(db_url, content, format="markdown", kind=None, uid="uid-a"):
    await init_db(db_url)
    await provision_user(uid, None)
    if kind:
        await create_session_row("s-1", uid)
        async with session_factory()() as db:
            doc = Document(
                user_id=uid, session_id="s-1", source="agent",
                format=format, kind=kind, title="t", content=content,
            )
            db.add(doc)
            await db.commit()
            return doc
    return await insert_upload_row(uid, "t", format, content)


def test_str_replace_unique_multi_and_replace_all(db_url):
    async def run():
        doc = await _seed(db_url, "plan A\nplan B\ndone")
        kw = dict(ceiling=CEILING, turn_id=1, session_id="s-x")

        # many occurrences without replace_all: refused
        assert (
            await str_replace_document(
                "uid-a", doc.id, "plan", "idea",
                replace_all=False, expected_occurrences=None, **kw,
            )
            is None
        )
        diag = await diagnose_edit_failure(
            "uid-a", doc.id, ceiling=CEILING,
            old_str="plan", new_str="idea", required=1,
        )
        assert diag.reason == "count_mismatch" and diag.occurrences == 2

        # zero occurrences
        assert (
            await str_replace_document(
                "uid-a", doc.id, "ghost", "x",
                replace_all=False, expected_occurrences=None, **kw,
            )
            is None
        )
        diag = await diagnose_edit_failure(
            "uid-a", doc.id, ceiling=CEILING,
            old_str="ghost", new_str="x", required=1,
        )
        assert diag.reason == "no_match"

        # replace_all with the verified count replaces every occurrence
        row = await str_replace_document(
            "uid-a", doc.id, "plan", "idea",
            replace_all=True, expected_occurrences=2, **kw,
        )
        assert row.content == "idea A\nidea B\ndone"
        assert row.updated_at is not None

        # wrong expectation: refused, count named
        assert (
            await str_replace_document(
                "uid-a", doc.id, "idea", "plan",
                replace_all=True, expected_occurrences=5, **kw,
            )
            is None
        )

        # unique match; empty new_str deletes
        row = await str_replace_document(
            "uid-a", doc.id, "done", "",
            replace_all=False, expected_occurrences=None, **kw,
        )
        assert row.content.endswith("idea B\n")

    asyncio.run(run())


def test_growth_gate_refuses_growth_but_allows_shrink(db_url):
    async def run():
        # over-ceiling row; the shrink target "A"*50 occurs exactly once
        over = await _seed(db_url, "A" * 50 + "y" * CEILING)
        kw = dict(ceiling=CEILING, turn_id=None, session_id="s-x")

        # growth on an over-ceiling row: refused (append always grows)
        assert (
            await append_document_content("uid-a", over.id, "\nmore", **kw)
            is None
        )
        diag = await diagnose_edit_failure(
            "uid-a", over.id, ceiling=CEILING, insert_text="more"
        )
        assert diag.reason == "over_ceiling"

        # a shrinking str_replace on the over-ceiling row SUCCEEDS
        row = await str_replace_document(
            "uid-a", over.id, "A" * 50, "",
            replace_all=False, expected_occurrences=None,
            ceiling=CEILING, turn_id=None, session_id="s-x",
        )
        assert row is not None and len(row.content) == CEILING

        # an under-ceiling row refuses an edit that would cross the ceiling
        small = await insert_upload_row("uid-a", "s", "text", "ab")
        assert (
            await append_document_content(
                "uid-a", small.id, "y" * CEILING, **kw
            )
            is None
        )
        # ...and accepts one that stays under
        row = await append_document_content("uid-a", small.id, "\ncd", **kw)
        assert row.content == "ab\ncd"

    asyncio.run(run())


def test_format_gate_and_diagnostic_precedence(db_url):
    """pdf is read-only in the UPDATE predicate itself, and the diagnostic
    names the NEAREST obstacle: a pdf with three matches steers on format,
    an ambiguous-and-over-ceiling edit steers on ambiguity (actionable now),
    never 'create a new one' for a shrink-after-narrowing."""

    async def run():
        pdf = await _seed(db_url, "plan plan plan", format="pdf")
        kw = dict(ceiling=CEILING, turn_id=None, session_id="s-x")
        assert (
            await str_replace_document(
                "uid-a", pdf.id, "plan", "idea",
                replace_all=False, expected_occurrences=None, **kw,
            )
            is None
        )
        diag = await diagnose_edit_failure(
            "uid-a", pdf.id, ceiling=CEILING,
            old_str="plan", new_str="idea", required=1,
        )
        assert diag.reason == "not_editable" and diag.format == "pdf"
        assert "pdf" not in EDITABLE_FORMATS

        # ambiguous AND over-ceiling: ambiguity wins the precedence
        multi = await _seed(db_url, "aa aa", format="markdown")
        diag = await diagnose_edit_failure(
            "uid-a", multi.id, ceiling=3,
            old_str="aa", new_str="a" * 500, required=1,
        )
        assert diag.reason == "count_mismatch"

        # unknown id: not_found before anything else
        diag = await diagnose_edit_failure(
            "uid-a", 999_999, ceiling=CEILING,
            old_str="x", new_str="y", required=1,
        )
        assert diag.reason == "not_found"

    asyncio.run(run())


def test_edit_events_carry_kind_or_format(db_url):
    """FR-32 amended: artifact_edited's detail is the kind for agent
    documents and the FORMAT for uploads (which have none) — never NULL."""

    async def run():
        upload = await _seed(db_url, "hello world", format="text")
        agent_doc = await _seed(db_url, "hello world", kind="summary")
        kw = dict(ceiling=CEILING, turn_id=7, session_id="s-edit")
        await str_replace_document(
            "uid-a", upload.id, "hello", "hi",
            replace_all=False, expected_occurrences=None, **kw,
        )
        await str_replace_document(
            "uid-a", agent_doc.id, "hello", "hi",
            replace_all=False, expected_occurrences=None, **kw,
        )
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
        details = sorted(e.detail for e in events)
        assert details == ["summary", "text"]
        assert all(e.session_id == "s-edit" and e.turn_id == 7 for e in events)

    asyncio.run(run())


def test_concurrent_append_and_str_replace_lose_neither(db_url):
    """FR-53 test (ii): both are single statements — an append racing a
    str_replace loses neither edit."""

    async def run():
        doc = await _seed(db_url, "alpha TARGET omega")
        kw = dict(ceiling=10_000, turn_id=None, session_id="s-x")
        await asyncio.gather(
            append_document_content("uid-a", doc.id, "\nappended", **kw),
            str_replace_document(
                "uid-a", doc.id, "TARGET", "replaced",
                replace_all=False, expected_occurrences=None, **kw,
            ),
        )
        async with session_factory()() as db:
            row = await db.get(Document, doc.id)
        assert "replaced" in row.content
        assert row.content.endswith("appended")

    asyncio.run(run())
