"""Sanitizer tests (SDD §8): markdown in → clean spoken text out."""

import asyncio

from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

from agent.sanitizer import IdentifierTextFilter, make_text_filters


def test_toggle_off_keeps_only_the_identifier_filter():
    # the markdown sanitizer is gated; the identifier filter is always on
    filters = make_text_filters(False)
    assert [type(f) for f in filters] == [IdentifierTextFilter]


def test_toggle_on_adds_markdown_filter():
    filters = make_text_filters(True)
    assert any(isinstance(f, MarkdownTextFilter) for f in filters)
    assert any(isinstance(f, IdentifierTextFilter) for f in filters)


def test_filter_strips_markdown():
    md_filter = next(
        f for f in make_text_filters(True) if isinstance(f, MarkdownTextFilter)
    )
    cleaned = asyncio.run(md_filter.filter("**Bold** and *italic* and `code` here."))
    assert "*" not in cleaned
    assert "`" not in cleaned
    for word in ("Bold", "italic", "code", "here"):
        assert word in cleaned


def test_identifier_filter_speaks_snake_case_as_words():
    cleaned = asyncio.run(
        IdentifierTextFilter().filter("Use the create_artifact tool now.")
    )
    assert "create artifact" in cleaned
    assert "_" not in cleaned
