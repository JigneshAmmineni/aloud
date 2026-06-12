"use client";

import type { SessionState } from "@/lib/useAloudSession";

/** Single prominent session button (FR-5): green Talk / grey busy / red End. */
export function SessionButton({
  state,
  onTalk,
  onEnd,
}: {
  state: SessionState;
  onTalk: () => void;
  onEnd: () => void;
}) {
  const busy = state === "connecting" || state === "ending";
  const label = state === "active" ? "End" : busy ? "" : "Talk";

  return (
    <button
      className={`session-btn ${state}`}
      disabled={busy}
      onClick={state === "active" ? onEnd : onTalk}
      aria-label={state === "active" ? "End session" : "Start session"}
    >
      {busy ? <span className="spinner" aria-hidden /> : label}
    </button>
  );
}
