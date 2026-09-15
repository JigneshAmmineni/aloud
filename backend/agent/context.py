"""FR-44: the context provider — the loop's ONLY source of messages.

The loop never assembles messages itself; it calls this seam. The v1
implementation is deliberately trivial and behavior-identical to today's
LLMContext assembly: system prompt (+ the FR-21 documents block) followed
by the linear in-session conversation. Feature 5 (context engine) replaces
the internals — sections, budgets, compression — behind the same five
methods, with no change to loop control flow; feature 6 fills a
retrieved-memory slot the same way.

Message format is the neutral one agent/providers.py translates
(system/user/assistant/tool roles, OpenAI-shaped) — the provider seam owns
translation, this seam owns assembly and bookkeeping.
"""

import json

from loguru import logger

from agent.providers import LLMToolCall

# FR-46: the interrupted mark's wire form — a sentinel suffix on the
# assistant text, deliberately model-visible (an extra metadata key is
# dropped or rejected across providers; an unmarked prefix reads as a
# complete answer). Owned here: the loop passes interrupted=True, the
# provider applies the mark.
INTERRUPTED_SUFFIX = " [cut off by the user here]"

# Locally computed, explicitly approximate token estimate (FR-44): chars/4,
# never a provider count_tokens round trip on the hot path.
_CHARS_PER_TOKEN = 4


class ContextProvider:
    """One per session. Owns the linear conversation the loop builds on."""

    def __init__(
        self,
        system_prompt: str,
        documents_block: str | None = None,
        *,
        session_id: str = "",
    ):
        self._messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if documents_block:
            self._messages.append({"role": "system", "content": documents_block})
        self._log = logger.bind(session_id=session_id, component="agent.context")

    def build(self, *, turn_id: int | None = None, step: int = 1) -> list[dict]:
        """The message list for one LLM call. Deterministic, no LLM, no IO.

        Returns copies: the caller may hold the list across appends (the
        FR-49 trace serializes it at enqueue) without seeing later
        mutations of the conversation."""
        built = []
        for msg in self._messages:
            copy = dict(msg)
            if "tool_calls" in copy:
                copy["tool_calls"] = [dict(c) for c in copy["tool_calls"]]
            built.append(copy)
        self._log.bind(
            event="context.built",
            turn_id=turn_id,
            step=step,
            approx_tokens=self._approx_tokens(),
        ).info("context built")
        return built

    def append_user(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

    def append_step(
        self,
        step_text: str,
        calls: list[LLMToolCall],
        results: list[dict],
        *,
        interrupted: bool = False,
    ) -> None:
        """One completed (or interrupted) tool step, appended ATOMICALLY:
        the assistant message carrying the step's text AND its tool calls,
        then every call's result, in step order — results[i] answers
        calls[i]. Orphan tool results are unsendable (Claude rejects them;
        Gemini defines functionResponse relative to the functionCall turn),
        so this is the only way a tool round enters the context."""
        if len(calls) != len(results):
            raise ValueError(
                f"append_step: {len(calls)} calls but {len(results)} results — "
                "every issued call needs a terminal-or-placeholder result"
            )
        text = step_text
        if interrupted and text:
            text += INTERRUPTED_SUFFIX
        self._messages.append(
            {
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in calls
                ],
            }
        )
        for call, result in zip(calls, results):
            self._messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": result,
                }
            )

    def update_tool_result(self, call_id: str, result: dict) -> bool:
        """FR-46's late-write landing path: replace a previously appended
        tool result IN PLACE (the in_progress placeholder a barge-in left)
        — never a tail append, which after the user's next turn would be
        exactly the orphaned tool_result append_step exists to prevent.
        Called from a detached write task: never raises — a miss logs and
        returns False."""
        for msg in reversed(self._messages):
            if msg.get("role") == "tool" and msg.get("tool_call_id") == call_id:
                msg["content"] = result
                return True
        self._log.bind(event="context.update_miss", call_id=call_id).warning(
            "late tool result found no placeholder to update"
        )
        return False

    def append_assistant(self, text: str, *, interrupted: bool = False) -> None:
        """The turn's closing text-only assistant entry (also FR-43's
        filler-alone path and FR-46's spoken-prefix capture)."""
        if interrupted and text:
            text += INTERRUPTED_SUFFIX
        self._messages.append({"role": "assistant", "content": text})

    def _approx_tokens(self) -> int:
        chars = 0
        for msg in self._messages:
            content = msg.get("content")
            if isinstance(content, str):
                chars += len(content)
            else:
                chars += len(json.dumps(content, default=str))
            for call in msg.get("tool_calls", []):
                chars += len(call["name"]) + len(
                    json.dumps(call["arguments"], default=str)
                )
        return chars // _CHARS_PER_TOKEN
