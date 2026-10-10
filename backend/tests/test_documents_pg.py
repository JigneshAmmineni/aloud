"""FR-53's dialect-bound pieces — Postgres-only lane (gated like
test_rls.py): insert's atomicity and bounds, search_documents (both
scopes), and the multi-match line enumeration. Unique rows per run: the
target DB persists between runs.
"""

import asyncio
import os
import uuid

import pytest

from db.documents_repo import (
    SEARCH_RESULTS_CAP,
    diagnose_edit_failure,
    insert_document_line,
    insert_upload_row,
    read_document_page,
    search_document,
    search_workspace,
)
from db.engine import init_db
from db.users_repo import provision_user

PG_URL = os.environ.get("RLS_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="RLS_TEST_DATABASE_URL not set (Postgres required)"
)

CEILING = 10_000


def _uid() -> str:
    return f"docpg-{uuid.uuid4()}"


def test_insert_line_at_start_middle_end_and_bounds():
    uid = _uid()

    async def run():
        await init_db(PG_URL)
        await provision_user(uid, None)
        doc = await insert_upload_row(uid, "t.md", "markdown", "one\ntwo\nthree")
        kw = dict(ceiling=CEILING, turn_id=None, session_id="s-x")

        row = await insert_document_line(uid, doc.id, 0, "zero", **kw)
        assert row.content == "zero\none\ntwo\nthree"
        row = await insert_document_line(uid, doc.id, 2, "mid", **kw)
        assert row.content == "zero\none\nmid\ntwo\nthree"
        row = await insert_document_line(uid, doc.id, 5, "last", **kw)
        assert row.content == "zero\none\nmid\ntwo\nthree\nlast"

        # out of range: refused, diagnostic names the bounds
        assert await insert_document_line(uid, doc.id, 99, "x", **kw) is None
        diag = await diagnose_edit_failure(
            uid, doc.id, ceiling=CEILING, insert_line=99, insert_text="x"
        )
        assert diag.reason == "bad_line" and diag.total_lines == 6

        # over-ceiling growth: refused
        assert (
            await insert_document_line(
                uid, doc.id, 0, "y" * CEILING, **kw
            )
            is None
        )

    asyncio.run(run())


def test_workspace_search_shapes_precedence_and_disclosure():
    uid = _uid()

    async def run():
        await init_db(PG_URL)
        await provision_user(uid, None)
        # title+content both hit -> content wins, title_matched carried
        both = await insert_upload_row(
            uid, "plan.md", "markdown", "intro\nthe plan is here\nplan again"
        )
        # title-only hit -> null line/snippet
        title_only = await insert_upload_row(uid, "plans.txt", "text", "nothing")
        # no hit
        await insert_upload_row(uid, "other.txt", "text", "unrelated")

        res = await search_workspace(uid, "plan")
        assert res.documents_matched == 2
        by_id = {h.id: h for h in res.hits}

        hit = by_id[both.id]
        assert hit.match == "content" and hit.title_matched is True
        assert hit.line == 2  # first content match
        assert "the plan is here" in hit.snippet  # stored content, verbatim
        assert hit.match_count == 2  # case-insensitive content occurrences

        t = by_id[title_only.id]
        assert t.match == "title" and t.line is None and t.snippet is None

        # literal query: wildcards and regex metacharacters never widen
        assert (await search_workspace(uid, "%")).documents_matched == 0
        assert (await search_workspace(uid, "plan (v2)")).documents_matched == 0

    asyncio.run(run())


def test_workspace_search_results_cap_discloses_truncation():
    uid = _uid()

    async def run():
        await init_db(PG_URL)
        await provision_user(uid, None)
        for i in range(SEARCH_RESULTS_CAP + 2):
            await insert_upload_row(uid, f"n{i}.txt", "text", "needle here")
        res = await search_workspace(uid, "needle")
        assert len(res.hits) == SEARCH_RESULTS_CAP
        assert res.documents_matched == SEARCH_RESULTS_CAP + 2  # not silent

    asyncio.run(run())


def test_single_document_search_two_engines_and_lines():
    uid = _uid()

    async def run():
        await init_db(PG_URL)
        await provision_user(uid, None)
        doc = await insert_upload_row(
            uid, "t.md", "markdown", "## Plan\nbody plan one\nmore\nplan plan"
        )
        res = await search_document(uid, doc.id, "plan")
        # mixed-case: 4 insensitive, 3 verbatim — the two engines disagree
        assert res.total_matches == 4
        assert res.exact_matches == 3
        assert [m.line for m in res.matches] == [1, 2, 4, 4]
        assert "Plan" in res.matches[0].snippet  # sliced from STORED content

        none = await search_document(uid, doc.id, "ghost")
        assert none.total_matches == 0 and none.matches == []

        # unowned id resolves to None (NFR-8 shape)
        other = _uid()
        await provision_user(other, None)
        assert await search_document(other, doc.id, "plan") is None

    asyncio.run(run())


def test_search_line_resolves_to_first_page_carrying_it():
    """Mandated test (vi): a match deep in a large document returns a line
    that read_document's line argument resolves to the first page carrying
    that line."""
    uid = _uid()

    async def run():
        await init_db(PG_URL)
        await provision_user(uid, None)
        filler = "\n".join(f"filler line {i}" for i in range(1200))
        content = filler + "\nthe NEEDLE sentence\n" + "tail\n" * 50
        doc = await insert_upload_row(uid, "big.txt", "text", content)

        res = await search_document(uid, doc.id, "needle")
        assert res.total_matches == 1
        line = res.matches[0].line
        page = await read_document_page(uid, doc.id, line=line)
        assert page.page > 1  # genuinely deep
        assert "NEEDLE" in page.rendered
        assert page.first_line <= line <= page.last_line

    asyncio.run(run())


def test_multi_match_diagnostic_names_verbatim_lines():
    uid = _uid()

    async def run():
        await init_db(PG_URL)
        await provision_user(uid, None)
        doc = await insert_upload_row(
            uid, "t.md", "markdown", "aaa\nxx\naaa\nyy\naaa"
        )
        diag = await diagnose_edit_failure(
            uid, doc.id, ceiling=CEILING, old_str="aaa", new_str="b", required=1
        )
        assert diag.reason == "count_mismatch"
        assert diag.occurrences == 3
        assert diag.match_lines == [1, 3, 5]  # the verbatim ones

    asyncio.run(run())


def test_workspace_search_never_returns_other_users_rows():
    uid_a, uid_b = _uid(), _uid()

    async def run():
        await init_db(PG_URL)
        await provision_user(uid_a, None)
        await provision_user(uid_b, None)
        await insert_upload_row(uid_b, "secret.txt", "text", "needle secret")
        res = await search_workspace(uid_a, "needle")
        assert res.hits == [] and res.documents_matched == 0

    asyncio.run(run())
