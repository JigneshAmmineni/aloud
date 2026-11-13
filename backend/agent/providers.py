"""THE provider swap point (SDD §2.4, C-2).

The only module that imports provider SDK / service classes. Each factory
branches on the configured provider name and returns a Pipecat service —
or, for the agent loop (FR-48), a thin streaming client speaking the
neutral contract defined here. Swapping a provider = add a branch here +
set an env var.
"""

import asyncio
import functools
import json
import uuid
from dataclasses import dataclass, field

import anthropic
from google import genai
from google.genai import types as genai_types
from loguru import logger
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService

from app.config import Settings


def make_stt(settings: Settings):
    if settings.stt_provider == "deepgram_flux":
        return DeepgramFluxSTTService(
            api_key=settings.deepgram_api_key,
            # Higher eot_threshold = Flux waits for more certainty the user is
            # done, sitting through brief pauses before ending the turn (softer
            # turn-taking). Confidence-based, not a fixed timer. Tune by ear.
            settings=DeepgramFluxSTTService.Settings(
                eot_threshold=settings.flux_eot_threshold
            ),
        )
    raise ValueError(f"Unknown STT_PROVIDER: {settings.stt_provider}")


def make_tts(settings: Settings, text_filters: list):
    if settings.tts_provider == "cartesia":
        return CartesiaTTSService(
            api_key=settings.cartesia_api_key,
            settings=CartesiaTTSService.Settings(
                model="sonic-3",
                voice=settings.cartesia_voice_id,
                # Sonic-3 speech rate; 1.0 = normal, lower = slower.
                generation_config=GenerationConfig(speed=settings.cartesia_speed),
            ),
            text_filters=text_filters,
        )
    raise ValueError(f"Unknown TTS_PROVIDER: {settings.tts_provider}")


# --------------------------------------------------------------------------
# Loop LLM client (FR-48): the agent loop's provider-agnostic streaming
# seam. The loop consumes ONLY the neutral types below; everything
# provider-specific lives in a pair of translation functions plus a client
# class per provider (ClaudeLoopClient, GeminiLoopClient), chosen by the
# factories at the bottom (C-2).
# --------------------------------------------------------------------------

# Finish-reason vocabulary (FR-48). INTERRUPTED is assigned by the LOOP
# when barge-in cancels consumption — a client can't observe its own
# consumer being cancelled — but it lives in this vocabulary so traces
# (FR-49) and loop logic share one spelling.
FINISH_STOP = "stop"
FINISH_BLOCKED = "blocked"
FINISH_TRUNCATED = "truncated"
FINISH_INTERRUPTED = "interrupted"


@dataclass
class LLMToolCall:
    """One tool call from the model. `id` is OUR correlation key — minted
    here when the provider supplies none (Gemini usually doesn't) — used by
    the context provider to pair results and update them in place (FR-46).
    Claude supplies its own and pairs results by it; it is never sent back
    to Gemini (results pair by name/order there)."""

    name: str
    arguments: dict
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")


@dataclass
class LLMTextDelta:
    text: str


@dataclass
class LLMUsage:
    """Token counts. Carried by LLMDone; a client MAY also yield one
    on its own, earlier, when the provider reports the input count up
    front (Claude does) — LLMDone's supersedes it. An early one is not
    model output: it does not mark the first token."""

    prompt_tokens: int
    completion_tokens: int


@dataclass
class LLMDone:
    """Terminal stream event: mapped finish reason + usage if reported."""

    finish_reason: str
    usage: LLMUsage | None


# Gemini finish reasons → FR-48 vocabulary. Everything that isn't a normal
# stop or a length cut is a blocked/failed generation for the loop's
# purposes (FR-42's empty-step rule branches on content, not this; the
# reason feeds logs and traces).
_GEMINI_FINISH_MAP = {
    "STOP": FINISH_STOP,
    "MAX_TOKENS": FINISH_TRUNCATED,
}


def _check_tool_choice(tool_choice: str) -> None:
    """FR-48 names a silent mistranslation of this directive as what
    would unguard the greeting: any spelling other than the two the
    loop uses must fail loudly, never fall through to "auto"."""
    if tool_choice not in ("auto", "none"):
        raise ValueError(f"unknown tool_choice: {tool_choice!r}")


def map_gemini_finish(raw: object, produced_output: bool) -> str:
    """`raw` is the last candidate finish_reason seen (enum or None). A
    stream that ended with no finish reason at all and no output is a
    prompt-level block (Gemini reports those via prompt_feedback, with no
    candidate); with output, treat as a normal stop."""
    if raw is None:
        return FINISH_STOP if produced_output else FINISH_BLOCKED
    name = getattr(raw, "name", None) or str(raw)
    return _GEMINI_FINISH_MAP.get(name, FINISH_BLOCKED)


def to_gemini_tools(tools: list[dict]) -> list[genai_types.Tool] | None:
    """Registry entries ({name, description, parameters: JSON schema}) →
    Gemini declarations. The SDK's pydantic models coerce plain JSON-schema
    dicts (lowercase types, enum, required) directly — verified against
    google-genai 1.x."""
    if not tools:
        return None
    return [
        genai_types.Tool(
            function_declarations=[
                genai_types.FunctionDeclaration(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters=t["parameters"],
                )
            ]
        )
        for t in tools
    ]


def _is_tool_response_content(content: genai_types.Content) -> bool:
    """The consecutive-tool-result merge predicate (load-bearing for the
    C-2 swap): a user-role content already carrying function responses —
    the shape Gemini's own function-calling loop emits."""
    return bool(
        content.role == "user"
        and content.parts
        and content.parts[0].function_response is not None
    )


def to_gemini_contents(
    messages: list[dict],
) -> tuple[str | None, list[genai_types.Content]]:
    """Neutral messages → (system_instruction, contents).

    Neutral roles: system (hoisted — Gemini takes it as config, not a
    content), user, assistant (text and/or tool_calls), tool (one result
    per call). Consecutive tool results merge into ONE user-role content —
    the shape Gemini's own function-calling loop emits (role='user' with
    function_response parts)."""
    system_parts: list[str] = []
    contents: list[genai_types.Content] = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            system_parts.append(msg["content"])
        elif role == "user":
            contents.append(
                genai_types.Content(
                    role="user", parts=[genai_types.Part(text=msg["content"])]
                )
            )
        elif role == "assistant":
            parts = []
            if msg.get("content"):
                parts.append(genai_types.Part(text=msg["content"]))
            for call in msg.get("tool_calls", []):
                parts.append(
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(
                            name=call["name"], args=call["arguments"]
                        )
                    )
                )
            contents.append(genai_types.Content(role="model", parts=parts))
        elif role == "tool":
            result = msg["content"]
            if not isinstance(result, dict):
                result = {"result": result}
            part = genai_types.Part.from_function_response(
                name=msg["name"], response=result
            )
            if contents and _is_tool_response_content(contents[-1]):
                contents[-1].parts.append(part)
            else:
                contents.append(genai_types.Content(role="user", parts=[part]))
        else:
            raise ValueError(f"unknown message role: {role}")
    return ("\n\n".join(system_parts) or None), contents


class GeminiLoopClient:
    """FR-48's thin streaming client, Gemini branch.

    One long-lived genai.Client per instance (per session): an unreferenced
    client is garbage-collected mid-stream while the lazy generator still
    runs — the spike's RuntimeError. Thinking is pinned off (ADR #4): the
    raw API defaults it ON for 2.5 Flash, costing ~5s TTFB and hidden
    tokens."""

    def __init__(self, api_key: str, model: str):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        max_output_tokens: int | None = None,
    ):
        """Yields LLMTextDelta / LLMToolCall events, then exactly one
        LLMDone. `tool_choice`: "auto" or "none" — "none" keeps the tools
        DECLARED but forbids selection (FR-42's greeting and forced final
        step; spike-verified against the live API). `max_output_tokens`
        bounds short classifier calls (the FR-60 judge); None leaves the
        model default."""
        _check_tool_choice(tool_choice)
        config = genai_types.GenerateContentConfig(
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        )
        if max_output_tokens is not None:
            config.max_output_tokens = max_output_tokens
        system_instruction, contents = to_gemini_contents(messages)
        if system_instruction:
            config.system_instruction = system_instruction
        gemini_tools = to_gemini_tools(tools or [])
        if gemini_tools:
            config.tools = gemini_tools
            if tool_choice == "none":
                config.tool_config = genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode="NONE"
                    )
                )

        produced = False
        raw_finish = None
        usage = None
        stream = await self._client.aio.models.generate_content_stream(
            model=self._model, contents=contents, config=config
        )
        async for chunk in stream:
            cand = (chunk.candidates or [None])[0]
            parts = (cand.content.parts if cand and cand.content else None) or []
            for part in parts:
                if part.text:
                    produced = True
                    yield LLMTextDelta(part.text)
                if part.function_call is not None:
                    produced = True
                    fc = part.function_call
                    call = LLMToolCall(name=fc.name, arguments=dict(fc.args or {}))
                    if fc.id:
                        call.id = fc.id
                    yield call
            if cand and cand.finish_reason:
                raw_finish = cand.finish_reason
            if chunk.usage_metadata:
                usage = LLMUsage(
                    prompt_tokens=chunk.usage_metadata.prompt_token_count or 0,
                    completion_tokens=chunk.usage_metadata.candidates_token_count
                    or 0,
                )
        yield LLMDone(map_gemini_finish(raw_finish, produced), usage)


# --------------------------------------------------------------------------
# Claude branch (Anthropic Messages API).
# --------------------------------------------------------------------------

_CLAUDE_FINISH_MAP = {
    "end_turn": FINISH_STOP,
    "tool_use": FINISH_STOP,
    "max_tokens": FINISH_TRUNCATED,
    "refusal": FINISH_BLOCKED,
}


def map_anthropic_finish(raw: str | None, produced_output: bool) -> str:
    """`raw` is the stream's stop_reason. Same shape as map_gemini_finish:
    anything that isn't a normal stop or a length cut is blocked, and a
    stream that ended without a stop_reason is judged by its output."""
    if raw is None:
        return FINISH_STOP if produced_output else FINISH_BLOCKED
    return _CLAUDE_FINISH_MAP.get(raw, FINISH_BLOCKED)


def to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Registry entries → Claude tool definitions (the JSON schema passes
    through as input_schema). eager_input_streaming stays OFF on purpose:
    without it the server validates each tool input and delivers it whole,
    which is the atomic-call contract the loop's filler timing relies on."""
    return [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t["parameters"],
        }
        for t in tools
    ]


# Claude requires the conversation to open with a user turn. Ours opens
# with the agent's greeting, because the greeting's trigger is ephemeral
# (sent for that one call, never appended to the context).
_OPENING_USER_TURN = "(The user just connected.)"


def _is_tool_result_message(message: dict) -> bool:
    """The consecutive-tool-result merge predicate. Plain user turns carry
    string content here, so list content only ever means tool results."""
    return message["role"] == "user" and isinstance(message["content"], list)


def to_anthropic_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Neutral messages → (system, messages).

    System messages hoist into the top-level system prompt — including
    the forced final step's ephemeral wrap-up line, which therefore
    lands ahead of the conversation instead of last (accepted, PR #24
    round 1: tool_choice none is the hard guarantee on that call;
    keeping it in place as a mid-conversation system message belongs
    with the prompt-caching restructure, ROADMAP feature 5). An assistant
    step becomes one message of a text block (when it has speakable text —
    Claude rejects empty and whitespace-only text blocks) plus one tool_use
    block per call. Every result of that step lands in ONE user message of
    tool_result blocks: Claude rejects a tool_use whose results are split
    across messages or missing.

    Two user messages in a row are sent as they are. The context
    produces them on ordinary turns (a turn cut before it spoke
    appends nothing; a barge-in during a tool round leaves the
    results message followed by the new turn), and the API combines
    consecutive same-role turns itself — verified live on both models,
    2026-11-12 (PR #24 round 3), and replayed by scripts/smoke_llm.py
    so a model swap re-checks it."""
    system_parts: list[str] = []
    out: list[dict] = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            system_parts.append(msg["content"])
        elif role == "user":
            out.append({"role": "user", "content": msg["content"]})
        elif role == "assistant":
            blocks: list[dict] = []
            if (msg.get("content") or "").strip():
                blocks.append({"type": "text", "text": msg["content"]})
            for call in msg.get("tool_calls", []):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["arguments"],
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            result = msg["content"]
            block = {
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": result
                if isinstance(result, str)
                else json.dumps(result, ensure_ascii=False, default=str),
            }
            # Failures only — the loop's own failing outcomes, minus one:
            # a gate refusal (status "refused") is steering to follow,
            # not an error to retry.
            if isinstance(result, dict) and result.get("status") in (
                "error",
                "timeout",
                "not_found",
            ):
                block["is_error"] = True
            if out and _is_tool_result_message(out[-1]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            raise ValueError(f"unknown message role: {role}")
    if out and out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": _OPENING_USER_TURN})
    return ("\n\n".join(system_parts) or None), out


# Per-model request fields. Thinking is OFF on both models (why, and what
# turning it on would take: CURRENT-ARCHITECTURE.md §2, "LLM models and
# thinking"). The spelling is per model and not interchangeable: each
# model answers 400 to the other's.
# Sonnet also opts into server-side fallback, which re-runs a declined
# request on the previous Sonnet; Haiku has no fallback and rejects the field.
_CLAUDE_MODELS = {
    "claude-sonnet-5-5": {
        "thinking": {"type": "between_tools"},
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    },
    "claude-haiku-5-5": {"thinking": {"type": "disabled"}},
}

# Not the SDK defaults (600s, 2 retries). Two different waits:
#
# - For the response to START: bounded tightly, with no retry. This is
#   a liveness check, not a limit on work: the first event arrives in
#   about 1.5s however long the response will be. A stalled or
#   rate-limited call must fail fast into the loop's spoken fallback,
#   because nothing else covers that silence (the filler arms only on a
#   tool call), and a retry lands in a conversation that has moved on.
# - Once it has started: NO time limit (owner decision, 2026-11-12). A
#   response takes as long as its output needs. Claude buffers a tool
#   call's input and sends NOTHING while writing it — measured 34s of
#   silence for an 11,700-character document — so any between-bytes
#   timeout is a cap on document length in disguise (8s killed that
#   call). A call that is slow for its output is found from its trace
#   (duration against tokens), not cut off. The accepted residual
#   (PR #24 round 3 proposed a 300s read timeout for it; declined,
#   since any such bound also cuts a long silent write): a
#   half-open connection — dropped mid-stream with no FIN — hangs
#   the turn with no fallback line and no trace row until the user
#   speaks again (which cancels and restarts) or the session ends.
_CLAUDE_FIRST_EVENT_TIMEOUT_S = 10.0
_CLAUDE_CONNECT_TIMEOUT_S = 5.0


@functools.lru_cache(maxsize=None)
def _anthropic_client(api_key: str) -> anthropic.AsyncAnthropic:
    """One SDK client, and so one connection pool, per key for the
    life of the process. Per-session clients were never closed, and
    the judge's separate pool was cold on the first gated create of
    every session."""
    return anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=anthropic.Timeout(None, connect=_CLAUDE_CONNECT_TIMEOUT_S),
        max_retries=0,
    )


# The cap when the caller sets none. Sized for a whole document arriving as
# one create_document input, not for a spoken reply.
_CLAUDE_MAX_TOKENS = 64000


class ClaudeLoopClient:
    """FR-48's thin streaming client, Claude branch.

    A thin per-session wrapper over the process-wide SDK client.
    Thinking blocks are never yielded and never
    replayed: with thinking off the only ones that arrive are Sonnet's
    longer between-tool-call notes, and the context this client is fed is
    edited in place between calls (FR-46, FR-61), which replayed thinking
    blocks do not survive."""

    def __init__(self, api_key: str, model: str, effort: str, session_id: str = ""):
        if model not in _CLAUDE_MODELS:
            raise ValueError(
                f"Unsupported Anthropic model: {model!r} — supported: "
                + ", ".join(_CLAUDE_MODELS)
            )
        self._client = _anthropic_client(api_key)
        self._model = model
        self._effort = effort
        self._log = logger.bind(session_id=session_id, component="agent.llm")

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        max_output_tokens: int | None = None,
    ):
        """Same contract as GeminiLoopClient.stream: LLMTextDelta /
        LLMToolCall events, then exactly one LLMDone — plus one early
        LLMUsage carrying the input-token count. A tool call is
        yielded when its block completes, carrying Claude's own id.
        Raises TimeoutError if the response has not started within
        _CLAUDE_FIRST_EVENT_TIMEOUT_S."""
        _check_tool_choice(tool_choice)
        system, claude_messages = to_anthropic_messages(messages)
        request = {
            "model": self._model,
            "max_tokens": max_output_tokens or _CLAUDE_MAX_TOKENS,
            "messages": claude_messages,
            "output_config": {"effort": self._effort},
            **_CLAUDE_MODELS[self._model],
        }
        if system:
            request["system"] = system
        if tools:
            request["tools"] = to_anthropic_tools(tools)
            if tool_choice == "none":
                request["tool_choice"] = {"type": "none"}

        produced = False
        stop_reason = None
        prompt_tokens = None
        completion_tokens = 0
        # The deadline covers only the wait for the response to START (the
        # request, its headers, the first event) and is lifted as soon as
        # anything arrives.
        async with asyncio.timeout(_CLAUDE_FIRST_EVENT_TIMEOUT_S) as first_event:
            async with self._client.beta.messages.stream(**request) as stream:
                async for event in stream:
                    first_event.reschedule(None)
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta" and event.delta.text:
                            produced = True
                            yield LLMTextDelta(event.delta.text)
                    elif event.type == "content_block_stop":
                        block = event.content_block
                        if block.type == "tool_use":
                            produced = True
                            yield LLMToolCall(
                                name=block.name,
                                arguments=dict(block.input),
                                id=block.id,
                            )
                        elif block.type == "thinking" and block.thinking:
                            # Count only: the note's text is conversation
                            # content and must not reach INFO logs (FR-39).
                            self._log.bind(
                                event="llm.thinking_discarded",
                                model=self._model,
                                chars=len(block.thinking),
                            ).info("between-tool-call note discarded (not spoken)")
                    elif event.type == "message_start":
                        prompt_tokens = event.message.usage.input_tokens
                        # Claude reports (and bills) the input before the
                        # first token: hand it over now so an interrupted
                        # call still records what it spent.
                        yield LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=0)
                    elif event.type == "message_delta":
                        stop_reason = event.delta.stop_reason
                        completion_tokens = event.usage.output_tokens
        usage = (
            LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
            if prompt_tokens is not None
            else None
        )
        yield LLMDone(map_anthropic_finish(stop_reason, produced), usage)


def _make_llm(settings: Settings, model: str, effort: str, session_id: str):
    if settings.llm_provider == "anthropic":
        return ClaudeLoopClient(
            api_key=settings.anthropic_api_key,
            model=model,
            effort=effort,
            session_id=session_id,
        )
    if settings.llm_provider == "google":
        if model.startswith("claude"):
            # the swap-back's leftover line (JUDGE_MODEL is the easy one
            # to miss): a judge that 404s refuses every create, silently
            raise ValueError(
                f"Anthropic model {model!r} configured under LLM_PROVIDER=google"
            )
        return GeminiLoopClient(api_key=settings.google_api_key, model=model)
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")


def make_loop_llm(settings: Settings, session_id: str = ""):
    """FR-48 factory: the streaming client the agent loop calls through."""
    return _make_llm(settings, settings.llm_model, settings.llm_effort, session_id)


def make_judge_llm(settings: Settings, session_id: str = ""):
    """The FR-60 gate's client: same seam, the smaller judge model. A
    one-word verdict needs no more than the lowest effort."""
    return _make_llm(settings, settings.judge_model, "low", session_id)


def validate_llm_config(settings: Settings) -> None:
    """Boot-time check (app/main.py): build both clients once so a
    provider/model pair no client can run stops the container from
    starting. Left to session start, the same mistake boots clean,
    passes the deploy health check, and then fails every session."""
    make_loop_llm(settings)
    make_judge_llm(settings)
