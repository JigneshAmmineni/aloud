"""FR-48 loop LLM client, Claude branch: translation + stream contract,
no network.

A fake stands in for the Anthropic SDK's streaming helper and replays the
event shapes the client reads (verified against anthropic 2.8). What these
tests cannot prove is that the live API accepts the request this client
builds — scripts/smoke_llm.py does that with a real key.
"""

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace

import pytest

from agent.providers import (
    FINISH_BLOCKED,
    FINISH_STOP,
    FINISH_TRUNCATED,
    ClaudeLoopClient,
    LLMDone,
    LLMTextDelta,
    LLMToolCall,
    map_anthropic_finish,
    to_anthropic_messages,
    to_anthropic_tools,
)

# ---- message translation ----


def test_system_messages_hoist_to_the_system_prompt():
    system, messages = to_anthropic_messages(
        [
            {"role": "system", "content": "base prompt"},
            {"role": "system", "content": "documents block"},
            {"role": "user", "content": "hi"},
            # the forced final step's ephemeral instruction arrives last
            {"role": "system", "content": "wrap up"},
        ]
    )
    assert system == "base prompt\n\ndocuments block\n\nwrap up"
    assert messages == [{"role": "user", "content": "hi"}]


def test_assistant_step_carries_text_and_calls_in_one_message():
    _, messages = to_anthropic_messages(
        [
            {"role": "user", "content": "write it up"},
            {
                "role": "assistant",
                "content": "On it.",
                "tool_calls": [
                    {"id": "toolu_1", "name": "edit_document", "arguments": {"id": 3}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_1",
                "name": "edit_document",
                "content": {"status": "edited"},
            },
        ]
    )
    assert messages[1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "On it."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "edit_document",
                "input": {"id": 3},
            },
        ],
    }


def test_all_results_of_one_step_land_in_one_user_message():
    _, messages = to_anthropic_messages(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "a", "name": "list_documents", "arguments": {}},
                    {"id": "b", "name": "read_document", "arguments": {"id": 1}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "name": "list_documents", "content": {"items": ["é"]}},
            {"role": "tool", "tool_call_id": "b", "name": "read_document", "content": "plain text"},
        ]
    )
    # user, assistant (calls only: the empty text produced no text block),
    # ONE user message answering both calls by id
    assert len(messages) == 3
    assert [b["type"] for b in messages[1]["content"]] == ["tool_use", "tool_use"]
    results = messages[2]
    assert results["role"] == "user"
    assert [b["tool_use_id"] for b in results["content"]] == ["a", "b"]
    assert json.loads(results["content"][0]["content"]) == {"items": ["é"]}
    assert "é" in results["content"][0]["content"]  # not \u-escaped
    assert results["content"][1]["content"] == "plain text"


def test_separate_steps_keep_separate_result_messages():
    step = lambda cid: [  # noqa: E731
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": cid, "name": "t", "arguments": {}}],
        },
        {"role": "tool", "tool_call_id": cid, "name": "t", "content": {}},
    ]
    _, messages = to_anthropic_messages(
        [{"role": "user", "content": "q"}, *step("a"), *step("b")]
    )
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant", "user"]
    assert [len(m["content"]) for m in messages[2::2]] == [1, 1]


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_unspeakable_assistant_text_is_dropped(text):
    # Claude rejects empty and whitespace-only text blocks; an interrupted
    # turn that spoke nothing leaves exactly such an entry in the context
    _, messages = to_anthropic_messages(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": text},
            {"role": "user", "content": "again"},
        ]
    )
    assert [m["role"] for m in messages] == ["user", "user"]


def test_conversation_opening_with_the_greeting_gets_a_user_turn_first():
    # the greeting's trigger is ephemeral, so from turn two on the stored
    # conversation opens with the assistant — which Claude rejects
    _, messages = to_anthropic_messages(
        [
            {"role": "system", "content": "base"},
            {"role": "assistant", "content": "Hi, what's on your mind?"},
            {"role": "user", "content": "the launch plan"},
        ]
    )
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"]  # a non-empty stand-in turn


def test_conversation_opening_with_a_user_turn_is_left_alone():
    _, messages = to_anthropic_messages([{"role": "user", "content": "hi"}])
    assert messages == [{"role": "user", "content": "hi"}]


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        to_anthropic_messages([{"role": "developer", "content": "x"}])


# ---- tool translation ----


def test_registry_schema_translates():
    schema = {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": ["summary"]}},
        "required": ["kind"],
    }
    assert to_anthropic_tools(
        [{"name": "create_document", "description": "desc", "parameters": schema}]
    ) == [{"name": "create_document", "description": "desc", "input_schema": schema}]


# ---- finish-reason mapping ----


def test_finish_reason_vocabulary():
    assert map_anthropic_finish("end_turn", True) == FINISH_STOP
    assert map_anthropic_finish("tool_use", True) == FINISH_STOP
    assert map_anthropic_finish("max_tokens", True) == FINISH_TRUNCATED
    for raw in ("refusal", "something_new"):
        assert map_anthropic_finish(raw, True) == FINISH_BLOCKED
    assert map_anthropic_finish(None, produced_output=False) == FINISH_BLOCKED
    assert map_anthropic_finish(None, produced_output=True) == FINISH_STOP


# ---- stream contract against a fake SDK ----


def _start(input_tokens=10):
    usage = SimpleNamespace(input_tokens=input_tokens)
    return SimpleNamespace(type="message_start", message=SimpleNamespace(usage=usage))


def _text(text):
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text)
    )


def _block_stop(**block):
    return SimpleNamespace(type="content_block_stop", content_block=SimpleNamespace(**block))


def _end(stop_reason="end_turn", output_tokens=4):
    return SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason=stop_reason),
        usage=SimpleNamespace(output_tokens=output_tokens),
    )


class _FakeStream:
    def __init__(self, events):
        self._events = events
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def __aiter__(self):
        for event in self._events:
            yield event


class _FakeAnthropic:
    """Stands in for AsyncAnthropic: client.beta.messages.stream(**request)."""

    def __init__(self, events):
        self.stream_obj = _FakeStream(events)
        self.request: dict = {}
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **request):
        self.request = request
        return self.stream_obj


def _client_with(events, model="claude-sonnet-5-5", effort="medium"):
    client = ClaudeLoopClient(api_key="a_fake", model=model, effort=effort)
    fake = _FakeAnthropic(events)
    client._client = fake
    return client, fake


def _collect(client, **kwargs):
    async def run():
        return [event async for event in client.stream(**kwargs)]

    return asyncio.run(run())


_USER = [{"role": "user", "content": "hi"}]
_TOOLS = [{"name": "t", "description": "", "parameters": {"type": "object", "properties": {}}}]


def test_stream_yields_deltas_then_done_with_usage():
    client, _ = _client_with(
        [
            _start(input_tokens=10),
            _text("Hello "),
            # the SDK helper also emits a derived event per text delta;
            # reading it too would speak every word twice
            SimpleNamespace(type="text", text="Hello ", snapshot="Hello "),
            _text("there."),
            _block_stop(type="text", text="Hello there."),
            _end("end_turn", output_tokens=4),
            SimpleNamespace(type="message_stop"),
        ]
    )
    events = _collect(client, messages=_USER)
    assert [e.text for e in events if isinstance(e, LLMTextDelta)] == ["Hello ", "there."]
    done = events[-1]
    assert isinstance(done, LLMDone)
    assert sum(isinstance(e, LLMDone) for e in events) == 1
    assert done.finish_reason == FINISH_STOP
    assert (done.usage.prompt_tokens, done.usage.completion_tokens) == (10, 4)


def test_tool_call_is_yielded_whole_with_claudes_own_id():
    client, _ = _client_with(
        [
            _start(),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json='{"ti'),
            ),
            _block_stop(type="tool_use", id="toolu_abc", name="create_document", input={"title": "t"}),
            _end("tool_use"),
        ]
    )
    events = _collect(client, messages=_USER, tools=_TOOLS)
    assert len(events) == 2  # the partial-JSON delta yields nothing
    call = events[0]
    assert isinstance(call, LLMToolCall)
    assert (call.name, call.arguments, call.id) == ("create_document", {"title": "t"}, "toolu_abc")
    assert events[1].finish_reason == FINISH_STOP


def test_between_tool_call_notes_are_neither_spoken_nor_counted_as_output():
    client, _ = _client_with(
        [
            _start(),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="thinking_delta", thinking="Checking the doc"),
            ),
            _block_stop(type="thinking", thinking="Checking the doc", signature="sig"),
            _end("refusal"),
        ]
    )
    events = _collect(client, messages=_USER)
    assert len(events) == 1
    assert events[0].finish_reason == FINISH_BLOCKED


def test_sonnet_request_shape():
    client, fake = _client_with([_start(), _text("hi"), _end()])
    _collect(
        client,
        messages=[{"role": "system", "content": "base"}, *_USER],
        tools=_TOOLS,
    )
    request = fake.request
    assert request["model"] == "claude-sonnet-5-5"
    assert request["system"] == "base"
    assert request["messages"] == _USER
    # thinking off, in the only spelling this model accepts
    assert request["thinking"] == {"type": "between_tools"}
    assert request["output_config"] == {"effort": "medium"}
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["fallbacks"] == "default"
    assert request["max_tokens"] == 64000
    assert [t["name"] for t in request["tools"]] == ["t"]
    assert "tool_choice" not in request  # auto is the API default


def test_haiku_request_shape_has_no_fallback_fields():
    client, fake = _client_with(
        [_start(), _text("YES"), _end()], model="claude-haiku-5-5", effort="low"
    )
    _collect(client, messages=_USER, max_output_tokens=8)
    request = fake.request
    assert request["thinking"] == {"type": "disabled"}
    assert request["output_config"] == {"effort": "low"}
    assert request["max_tokens"] == 8
    # Haiku has no server-side fallback and rejects the field
    assert "fallbacks" not in request and "betas" not in request
    assert "system" not in request and "tools" not in request


def test_tool_choice_none_declares_tools_but_forbids_selection():
    client, fake = _client_with([_start(), _text("hi"), _end()])
    _collect(client, messages=_USER, tools=_TOOLS, tool_choice="none")
    assert fake.request["tools"]  # still declared — the portable form (FR-42)
    assert fake.request["tool_choice"] == {"type": "none"}


def test_truncated_and_empty_streams_report_their_finish():
    client, _ = _client_with([_start(), _text("cut"), _end("max_tokens")])
    assert _collect(client, messages=_USER)[-1].finish_reason == FINISH_TRUNCATED
    client, _ = _client_with([])
    events = _collect(client, messages=_USER)
    assert len(events) == 1
    assert events[0].finish_reason == FINISH_BLOCKED
    assert events[0].usage is None


def test_abandoning_the_stream_closes_the_provider_connection():
    # barge-in: the loop stops consuming mid-stream (aclosing); the SDK
    # stream — and its HTTP connection — must close then, not at GC time
    client, fake = _client_with([_start(), _text("one "), _text("two"), _end()])

    async def run():
        async with aclosing(client.stream(messages=_USER)) as stream:
            async for _ in stream:
                break
        return fake.stream_obj.closed

    assert asyncio.run(run()) is True


def test_unsupported_model_fails_at_construction():
    with pytest.raises(ValueError, match="claude-sonnet-5-5"):
        ClaudeLoopClient(api_key="a_fake", model="claude-opus-5-5", effort="medium")
