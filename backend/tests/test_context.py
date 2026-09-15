"""FR-44 context provider tests — the message-shape rules the C-2 swap
depends on, no provider needed. Tests (i) and (ii) are the FR's mandated
pair; the rest pin the update-in-place path and the interrupted sentinel.
"""

import pytest

from agent.context import INTERRUPTED_SUFFIX, ContextProvider
from agent.providers import LLMToolCall


def _provider(**kwargs):
    return ContextProvider("base prompt", session_id="s-test", **kwargs)


def test_tool_step_shape_calls_immediately_before_results():
    """Mandated (i): after a tool step, build() places the assistant
    message carrying the calls immediately before its results, one result
    per issued call, in step order."""
    ctx = _provider()
    ctx.append_user("write it up and list my artifacts")
    calls = [
        LLMToolCall(name="create_artifact", arguments={"title": "t"}, id="c1"),
        LLMToolCall(name="list_artifacts", arguments={}, id="c2"),
    ]
    ctx.append_step(
        "let me do both—",
        calls,
        [{"status": "created"}, {"items": []}],
    )
    built = ctx.build()
    assistant = built[2]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "let me do both—"
    assert [c["id"] for c in assistant["tool_calls"]] == ["c1", "c2"]
    assert built[3] == {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "create_artifact",
        "content": {"status": "created"},
    }
    assert built[4]["tool_call_id"] == "c2"


def test_no_tool_context_matches_todays_llmcontext():
    """Mandated (ii): a no-tool built context matches today's LLMContext
    message list — the behavior-identical claim, asserted not assumed."""
    from pipecat.processors.aggregators.llm_context import LLMContext

    today = LLMContext(
        messages=[
            {"role": "system", "content": "base prompt"},
            {"role": "system", "content": "docs block"},
        ]
    )
    today.add_message({"role": "user", "content": "hello"})
    today.add_message({"role": "assistant", "content": "hi there"})

    ctx = ContextProvider("base prompt", "docs block", session_id="s")
    ctx.append_user("hello")
    ctx.append_assistant("hi there")
    assert ctx.build() == today.get_messages()


def test_update_tool_result_in_place():
    ctx = _provider()
    ctx.append_user("save this")
    call = LLMToolCall(name="create_artifact", arguments={}, id="w1")
    ctx.append_step("on it—", [call], [{"status": "in_progress"}])
    ctx.append_user("next turn already")

    assert ctx.update_tool_result("w1", {"status": "created", "id": 7})
    built = ctx.build()
    tool_msgs = [m for m in built if m["role"] == "tool"]
    assert tool_msgs == [
        {
            "role": "tool",
            "tool_call_id": "w1",
            "name": "create_artifact",
            "content": {"status": "created", "id": 7},
        }
    ]
    # no tail append: the update replaced the placeholder where it stood
    assert built[-1] == {"role": "user", "content": "next turn already"}


def test_update_miss_returns_false_never_raises():
    assert _provider().update_tool_result("ghost", {"status": "created"}) is False


def test_interrupted_sentinel_on_both_append_paths():
    ctx = _provider()
    ctx.append_assistant("I was saying", interrupted=True)
    call = LLMToolCall(name="read_artifact", arguments={}, id="r1")
    ctx.append_step("checking that", [call], [{"status": "cancelled"}], interrupted=True)
    built = ctx.build()
    assert built[1]["content"] == "I was saying" + INTERRUPTED_SUFFIX
    assert built[2]["content"] == "checking that" + INTERRUPTED_SUFFIX
    # an empty prefix stays empty — the sentinel marks spoken words, not silence
    ctx2 = _provider()
    ctx2.append_assistant("", interrupted=True)
    assert ctx2.build()[1]["content"] == ""


def test_step_requires_result_per_call():
    ctx = _provider()
    with pytest.raises(ValueError):
        ctx.append_step(
            "", [LLMToolCall(name="t", arguments={}, id="x")], []
        )


def test_build_returns_copies():
    ctx = _provider()
    ctx.append_user("hi")
    ctx.append_step(
        "ok", [LLMToolCall(name="t", arguments={"a": 1}, id="c")], [{"ok": True}]
    )
    snapshot = ctx.build()
    ctx.append_user("later turn")
    ctx.update_tool_result("c", {"ok": False})
    assert len(snapshot) == 4  # unchanged by the later append
    assert snapshot[2]["tool_calls"][0]["id"] == "c"
    assert snapshot[3]["content"] == {"ok": True}  # pre-update result
