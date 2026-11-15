/**
 * §4.15 turn checks, the client's two small halves.
 *
 * 1. The indicator (FR-6/FR-64). "Thinking" normally ends when the agent
 *    starts speaking. On a turn the agent chooses not to answer it never
 *    does, so the server says so with one `turn.silent` message and the
 *    indicator goes back to Listening — without it a silent turn looks
 *    like a hung session.
 * 2. The admin drill-down's outcome column (FR-36/FR-70): what the checks
 *    decided for each turn, from the usage event's `detail`. Usage only,
 *    never content (NFR-9).
 */

import type { VoiceMode } from "./useAloudSession";

/** The mode a server message moves the indicator to, or null for none. */
export function modeForServerMessage(type: unknown): VoiceMode | null {
  return type === "turn.silent" ? "listening" : null;
}

const LABELS: Record<string, string> = {
  silent_mid_thought: "silent: mid-thought",
  silent_nothing_asked: "silent: nothing asked",
  talk_only: "talk only",
  affirm: "affirm",
  edit_and_speak: "edit and speak",
  plain: "plain",
  interrupted: "check interrupted",
  bypass: "check unanswered",
};

/** "talk_only+affirm+bypass" → "talk only, affirm (check unanswered)".
 * A turn with no check event (the greeting, sessions before §4.15, a
 * switch that was off) shows "—". Unknown parts pass through as written,
 * so a future outcome is visible rather than hidden. */
export function fmtOutcome(outcome: string | null | undefined): string {
  if (!outcome) return "—";
  const parts = outcome.split("+");
  const bypass = parts.includes("bypass");
  const named = parts
    .filter((p) => p !== "bypass")
    .map((p) => LABELS[p] ?? p)
    .join(", ");
  return bypass ? `${named} (${LABELS.bypass})` : named;
}
