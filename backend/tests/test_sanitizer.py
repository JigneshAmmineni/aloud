"""Sanitizer tests (SDD §8): markdown in → clean spoken text out."""

import asyncio

from agent.sanitizer import make_text_filters


def test_toggle_off_returns_no_filters():
    assert make_text_filters(False) == []


def test_toggle_on_returns_one_filter():
    filters = make_text_filters(True)
    assert len(filters) == 1


def test_filter_strips_markdown():
    (md_filter,) = make_text_filters(True)
    cleaned = asyncio.run(md_filter.filter("**Bold** and *italic* and `code` here."))
    assert "*" not in cleaned
    assert "`" not in cleaned
    for word in ("Bold", "italic", "code", "here"):
        assert word in cleaned
