"""THE provider swap point (SDD §2.4, C-2).

The only module that imports provider SDK / service classes. Each factory
branches on the configured provider name and returns a Pipecat service —
or, for the agent loop (FR-48), a thin streaming client speaking the
neutral contract defined here. Swapping a provider = add a branch here +
set an env var.
"""

import uuid
from dataclasses import dataclass, field

from google import genai
from google.genai import types as genai_types
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
# seam. The loop consumes ONLY the neutral types below; everything Gemini
# lives in the translation functions and GeminiLoopClient. The Claude swap
# is a second client class + factory branch — zero loop changes (C-2).
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
    the context provider to pair results and update them in place (FR-46);
    it is never sent back to Gemini (results pair by name/order there)."""

    name: str
    arguments: dict
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")


@dataclass
class LLMTextDelta:
    text: str


@dataclass
class LLMUsage:
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
    ):
        """Yields LLMTextDelta / LLMToolCall events, then exactly one
        LLMDone. `tool_choice`: "auto" or "none" — "none" keeps the tools
        DECLARED but forbids selection (FR-42's greeting and forced final
        step; spike-verified against the live API)."""
        config = genai_types.GenerateContentConfig(
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        )
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


def make_loop_llm(settings: Settings):
    """FR-48 factory: the streaming client the agent loop calls through."""
    if settings.llm_provider == "google":
        return GeminiLoopClient(
            api_key=settings.google_api_key, model=settings.llm_model
        )
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider}")
