"""FR-48 spike: prove the provider-agnostic client's contract against the
live Gemini API before the loop is built on it, and answer FR-43's design
question. Kept as a reference artifact after the client shipped.

STATED EXEMPTION from the provider-seam constraint (CLAUDE.md): this is a
local-only diagnostic in scripts/, never imported by runtime code — the
only SDK usage outside agent/providers.py, by design.

Run inside the backend container:

    docker compose run --rm backend python scripts/spike_gemini_stream.py

Pass/fail criteria (FR-48):
  1. Streamed text deltas work.
  2. Native function calling works (create_artifact-shaped declaration).
  3. Usage metadata (prompt/completion tokens) is available on the stream.
  4. A finish reason is available.
  5. tool_choice NONE with tools still DECLARED yields a text-only
     response (FR-42's forced final step, the portable form).
  6. THE FR-43 DECIDER: are function calls delivered incrementally
     (args split across chunks -> first-delta filler trigger exists) or
     atomically (whole call in one chunk -> the trigger is call arrival,
     and the measured pre-arrival gap is the dead air no backstop covers)?

Local-only diagnostic; prints artifact content it asked Gemini to invent.
"""

import os
import time

from google import genai
from google.genai import types

MODEL = os.getenv("LLM_MODEL") or "gemini-2.5-flash"

# One long-lived client: an inline genai.Client() is garbage-collected
# (closing its httpx client) while the lazy stream generator still runs.
CLIENT = genai.Client()

# Production runs thinking-disabled (ADR #4, latency); measure what we ship.
NO_THINKING = types.ThinkingConfig(thinking_budget=0)

CREATE_ARTIFACT = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="create_artifact",
            description=(
                "Write up an artifact of the conversation and put it on the "
                "user's screen: a structured summary, a list of action "
                "items, or a cleaned-up version of the user's idea."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "title": types.Schema(type=types.Type.STRING),
                    "kind": types.Schema(
                        type=types.Type.STRING,
                        enum=["summary", "action_items", "cleaned_idea"],
                    ),
                    "content": types.Schema(type=types.Type.STRING),
                },
                required=["title", "kind", "content"],
            ),
        )
    ]
)

# Mimics a real "write that up" turn: enough conversational substance that
# the artifact body is a typical multi-hundred-token generation — the
# args-generation time IS the FR-43 atomic-branch measurement.
TOOL_PROMPT = (
    "We just talked through my plan to open a small bakery: sourdough "
    "focus, farmers-market stall first year, lease a storefront in year "
    "two, funding from savings plus a small loan, biggest risks are "
    "morning labor and flour costs. Please write that up as a structured "
    "summary artifact for my screen."
)


def run_stream(label, config, prompt):
    print(f"\n=== {label} ===")
    t0 = time.monotonic()
    first_chunk = first_text = first_call = last = None
    call_chunks = 0
    call_args_events = []  # (elapsed_ms, cumulative_args_chars) per chunk
    text_chars = 0
    finish = usage = None

    for chunk in CLIENT.models.generate_content_stream(
        model=MODEL, contents=prompt, config=config
    ):
        now = time.monotonic()
        ms = round((now - t0) * 1000)
        last = ms
        if first_chunk is None:
            first_chunk = ms
        cand = (chunk.candidates or [None])[0]
        parts = (cand.content.parts if cand and cand.content else None) or []
        for part in parts:
            if getattr(part, "text", None):
                if first_text is None:
                    first_text = ms
                text_chars += len(part.text)
            fc = getattr(part, "function_call", None)
            if fc is not None:
                if first_call is None:
                    first_call = ms
                call_chunks += 1
                args_len = len(str(getattr(fc, "args", "") or ""))
                call_args_events.append((ms, args_len))
        if cand and cand.finish_reason:
            finish = cand.finish_reason
        if chunk.usage_metadata:
            usage = chunk.usage_metadata

    print(f"model={MODEL}")
    print(f"first chunk (TTFB):      {first_chunk} ms")
    print(f"first TEXT delta:        {first_text} ms  ({text_chars} chars total)")
    print(f"first FUNCTION_CALL:     {first_call} ms")
    print(f"stream complete:         {last} ms")
    print(f"function_call chunks:    {call_chunks}")
    if call_args_events:
        print(f"call args per chunk:     {call_args_events}")
        verdict = (
            "ATOMIC (whole call in one chunk — FR-43 trigger = call "
            "arrival; the gap before it is the unbackstopped dead air)"
            if call_chunks == 1
            else "INCREMENTAL (args split across chunks — FR-43 "
            "first-delta trigger exists)"
        )
        print(f"FR-43 DELIVERY VERDICT:  {verdict}")
    print(f"finish_reason:           {finish}")
    if usage:
        print(
            "usage: prompt="
            f"{usage.prompt_token_count} candidates="
            f"{usage.candidates_token_count} total={usage.total_token_count}"
        )
    else:
        print("usage: NOT AVAILABLE on stream")


def main():
    # 1+3+4: plain streamed text with usage + finish reason
    run_stream(
        "A. plain text turn (criteria 1, 3, 4)",
        types.GenerateContentConfig(thinking_config=NO_THINKING),
        "In two short sentences, what makes sourdough different from "
        "regular bread?",
    )

    # 2+6: the tool turn — the decider
    run_stream(
        "B. tool turn (criteria 2, 6 — the FR-43 decider + dead-air measure)",
        types.GenerateContentConfig(tools=[CREATE_ARTIFACT], thinking_config=NO_THINKING),
        TOOL_PROMPT,
    )

    # 5: tools declared, selection forbidden (FR-42 forced final step)
    run_stream(
        "C. tools declared, mode NONE (criterion 5)",
        types.GenerateContentConfig(
            tools=[CREATE_ARTIFACT],
            thinking_config=NO_THINKING,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="NONE")
            ),
        ),
        TOOL_PROMPT,
    )


if __name__ == "__main__":
    main()
