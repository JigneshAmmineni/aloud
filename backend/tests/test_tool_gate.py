"""FR-60: the create gate's judge, unit level — verdict parsing, the
no-tools invariant, evidence bounding, and the timeout the loop fails
closed on. The loop-level refusal path is pinned in test_agent_loop.py.
"""

import asyncio

import pytest

import agent.gate as gate_mod
from agent.gate import _evidence, judge_allows, judge_messages
from agent.providers import LLMDone, LLMTextDelta, LLMUsage


class OneShotLLM:
    """A scripted FR-48 client for one judge call; floats sleep."""

    def __init__(self, events):
        self._events = events
        self.calls: list[dict] = []

    async def stream(
        self, messages, tools=None, tool_choice="auto", max_output_tokens=None
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "max_output_tokens": max_output_tokens,
            }
        )
        for e in self._events:
            if isinstance(e, (int, float)):
                await asyncio.sleep(e)
            else:
                yield e


def test_yes_allows_reports_usage_and_declares_no_tools():
    llm = OneShotLLM([LLMTextDelta("YES"), LLMDone("stop", LLMUsage(50, 1))])
    allowed, usage, verdict = asyncio.run(
        judge_allows(
            llm,
            judge_messages(
                "create_document",
                {"title": "Packing list"},
                ["make me a packing list for the trip"],
            ),
        )
    )
    assert allowed is True
    assert usage.prompt_tokens == 50
    # the judge must never recurse into tool use, and its output is capped
    assert llm.calls[0]["tools"] is None
    assert llm.calls[0]["max_output_tokens"] == 8


def test_no_refuses():
    llm = OneShotLLM([LLMTextDelta("NO"), LLMDone("stop", None)])
    allowed, _, _ = asyncio.run(
        judge_allows(
            llm,
            judge_messages(
                "create_document",
                {"title": "Notes"},
                ["can you note that detail down"],
            ),
        )
    )
    assert allowed is False


def test_anything_but_a_clear_yes_is_a_no():
    # a chatty judge that didn't follow the one-word contract must not
    # be parsed optimistically into an allow
    llm = OneShotLLM(
        [LLMTextDelta("Sure, creating it now!"), LLMDone("stop", None)]
    )
    allowed, _, _ = asyncio.run(
        judge_allows(llm, judge_messages("create_document", {}, ["hm"]))
    )
    assert allowed is False


def test_judge_timeout_propagates_for_the_loop_to_fail_closed(monkeypatch):
    monkeypatch.setattr(gate_mod, "JUDGE_TIMEOUT_S", 0.05)
    llm = OneShotLLM([60.0, LLMTextDelta("YES"), LLMDone("stop", None)])
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            judge_allows(llm, judge_messages("create_document", {}, ["x"]))
        )


def test_evidence_is_bounded_and_quotes_only_recent_turns():
    turns = [f"turn {i} " + "x" * 1000 for i in range(10)]
    text = _evidence("create_document", {"title": "t" * 500}, turns)
    assert "turn 9" in text and "turn 6" in text  # the last four
    assert "turn 5" not in text
    # per-turn and title caps hold: no unbounded transcript in the prompt
    assert len(text) < 4 * 700 + 400


def test_evidence_includes_the_assistants_offer_for_the_yes_to_answer():
    """Round-2 blocker: without the assistant's own question in the
    evidence, the ask-then-yes recovery can never pass the gate — the
    judge sees a bare 'yes' with its referent stripped."""
    msgs = judge_messages(
        "create_document",
        {"title": "Trip ideas"},
        ["put that somewhere separate", "yes"],
        assistant_text="Do you want a new document for this?",
    )
    user_block = msgs[1]["content"]
    assert "Do you want a new document for this?" in user_block
    assert "'yes'" in user_block
    # and without one, the block simply omits the line
    bare = judge_messages("create_document", {}, ["yes"])[1]["content"]
    assert "preceding reply" not in bare
