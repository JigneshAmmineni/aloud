"""FR-48 loop LLM client: translation + stream contract, no network.

The live-API behaviors (streaming, atomic call delivery, usage metadata,
mode NONE) were proven by the mandated spike (scripts/spike_gemini_stream.py);
these tests pin the pure translation layer and the event contract the loop
is built against.
"""

import asyncio
from types import SimpleNamespace

import pytest

from agent.providers import (
    FINISH_BLOCKED,
    FINISH_STOP,
    FINISH_TRUNCATED,
    GeminiLoopClient,
    LLMDone,
    LLMTextDelta,
    LLMToolCall,
    map_gemini_finish,
    to_gemini_contents,
    to_gemini_tools,
)

# ---- message translation ----


def test_system_messages_hoist_to_instruction():
    system, contents = to_gemini_contents(
        [
            {"role": "system", "content": "base prompt"},
            {"role": "system", "content": "documents block"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert system == "base prompt\n\ndocuments block"
    assert len(contents) == 1
    assert contents[0].role == "user"
    assert contents[0].parts[0].text == "hi"


def test_assistant_step_carries_text_and_calls_in_one_content():
    _, contents = to_gemini_contents(
        [
            {"role": "user", "content": "write it up"},
            {
                "role": "assistant",
                "content": "let me write that up—",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "create_artifact",
                        "arguments": {"title": "t"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "create_artifact",
                "content": {"status": "created"},
            },
        ]
    )
    model = contents[1]
    assert model.role == "model"
    assert model.parts[0].text == "let me write that up—"
    assert model.parts[1].function_call.name == "create_artifact"
    assert dict(model.parts[1].function_call.args) == {"title": "t"}


def test_consecutive_tool_results_merge_into_one_user_content():
    _, contents = to_gemini_contents(
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "a", "name": "list_artifacts", "arguments": {}},
                    {"id": "b", "name": "read_artifact", "arguments": {"id": 1}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "name": "list_artifacts", "content": {"items": []}},
            {"role": "tool", "tool_call_id": "b", "name": "read_artifact", "content": "plain text"},
        ]
    )
    # user, model, ONE merged function-response content
    assert len(contents) == 3
    responses = contents[2]
    assert responses.role == "user"
    assert len(responses.parts) == 2
    assert responses.parts[0].function_response.name == "list_artifacts"
    # non-dict results are wrapped (FunctionResponse.response must be a dict)
    assert responses.parts[1].function_response.response == {"result": "plain text"}


def test_tool_result_after_user_turn_does_not_merge_backward():
    # a tool result must never glue onto a plain user message
    _, contents = to_gemini_contents(
        [
            {"role": "user", "content": "q"},
            {"role": "tool", "tool_call_id": "a", "name": "t", "content": {}},
        ]
    )
    assert len(contents) == 2
    assert contents[1].parts[0].function_response is not None


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        to_gemini_contents([{"role": "developer", "content": "x"}])


# ---- tool translation ----


def test_registry_schema_translates():
    tools = to_gemini_tools(
        [
            {
                "name": "create_artifact",
                "description": "desc",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["summary"]},
                    },
                    "required": ["kind"],
                },
            }
        ]
    )
    decl = tools[0].function_declarations[0]
    assert decl.name == "create_artifact"
    assert decl.parameters.properties["kind"].enum == ["summary"]
    assert decl.parameters.required == ["kind"]


def test_no_tools_is_none():
    assert to_gemini_tools([]) is None


# ---- finish-reason mapping ----


def test_finish_reason_vocabulary():
    stop = SimpleNamespace(name="STOP")
    assert map_gemini_finish(stop, produced_output=True) == FINISH_STOP
    assert (
        map_gemini_finish(SimpleNamespace(name="MAX_TOKENS"), True)
        == FINISH_TRUNCATED
    )
    for raw in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT", "OTHER"):
        assert map_gemini_finish(SimpleNamespace(name=raw), True) == FINISH_BLOCKED
    # no candidate finish at all: prompt-level block if nothing came out
    assert map_gemini_finish(None, produced_output=False) == FINISH_BLOCKED
    assert map_gemini_finish(None, produced_output=True) == FINISH_STOP


# ---- stream contract against a fake SDK ----


def _chunk(parts=None, finish=None, usage=None):
    cand = SimpleNamespace(
        content=SimpleNamespace(parts=parts or []),
        finish_reason=finish,
    )
    return SimpleNamespace(candidates=[cand], usage_metadata=usage)


def _text_part(text):
    return SimpleNamespace(text=text, function_call=None)


def _call_part(name, args, call_id=None):
    return SimpleNamespace(
        text=None,
        function_call=SimpleNamespace(name=name, args=args, id=call_id),
    )


class _FakeAio:
    """Stands in for genai's client.aio.models; records the config."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.seen = {}

    @property
    def aio(self):
        return SimpleNamespace(models=SimpleNamespace(generate_content_stream=self._stream))

    async def _stream(self, *, model, contents, config):
        self.seen = {"model": model, "contents": contents, "config": config}

        async def gen():
            for c in self._chunks:
                yield c

        return gen()


def _client_with(chunks):
    client = GeminiLoopClient.__new__(GeminiLoopClient)
    fake = _FakeAio(chunks)
    client._client = fake
    client._model = "gemini-test"
    return client, fake


def _collect(client, **kwargs):
    async def run():
        return [event async for event in client.stream(**kwargs)]

    return asyncio.run(run())


def test_stream_yields_deltas_then_done():
    usage = SimpleNamespace(prompt_token_count=10, candidates_token_count=4)
    client, _ = _client_with(
        [
            _chunk([_text_part("Hello ")]),
            _chunk([_text_part("there.")], finish=SimpleNamespace(name="STOP"), usage=usage),
        ]
    )
    events = _collect(client, messages=[{"role": "user", "content": "hi"}])
    assert [e.text for e in events[:2] if isinstance(e, LLMTextDelta)] == [
        "Hello ",
        "there.",
    ]
    done = events[-1]
    assert isinstance(done, LLMDone)
    assert done.finish_reason == FINISH_STOP
    assert (done.usage.prompt_tokens, done.usage.completion_tokens) == (10, 4)


def test_stream_yields_tool_call_with_minted_id():
    client, _ = _client_with(
        [
            _chunk(
                [_call_part("create_artifact", {"title": "t"})],
                finish=SimpleNamespace(name="STOP"),
            )
        ]
    )
    events = _collect(
        client,
        messages=[{"role": "user", "content": "write it"}],
        tools=[{"name": "create_artifact", "description": "", "parameters": {"type": "object", "properties": {}}}],
    )
    call = events[0]
    assert isinstance(call, LLMToolCall)
    assert call.name == "create_artifact"
    assert call.arguments == {"title": "t"}
    assert call.id  # minted when Gemini supplies none


def test_tool_choice_none_declares_tools_but_forbids_selection():
    tools = [{"name": "t", "description": "", "parameters": {"type": "object", "properties": {}}}]
    client, fake = _client_with([_chunk([_text_part("hi")], finish=SimpleNamespace(name="STOP"))])
    _collect(
        client,
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice="none",
    )
    config = fake.seen["config"]
    assert config.tools  # still declared — the portable form (FR-42)
    assert config.tool_config.function_calling_config.mode == "NONE"
    # thinking pinned off on every call (ADR #4)
    assert config.thinking_config.thinking_budget == 0


def test_tool_choice_auto_sets_no_tool_config():
    tools = [{"name": "t", "description": "", "parameters": {"type": "object", "properties": {}}}]
    client, fake = _client_with([_chunk([_text_part("hi")], finish=SimpleNamespace(name="STOP"))])
    _collect(
        client, messages=[{"role": "user", "content": "hi"}], tools=tools
    )
    assert fake.seen["config"].tool_config is None


def test_empty_blocked_stream_reports_blocked():
    client, _ = _client_with([SimpleNamespace(candidates=None, usage_metadata=None)])
    events = _collect(client, messages=[{"role": "user", "content": "hi"}])
    assert len(events) == 1
    assert events[0].finish_reason == FINISH_BLOCKED
