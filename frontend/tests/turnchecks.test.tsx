/**
 * §4.15's two client-side mandated tests:
 * - FR-64/FR-6: a `turn.silent` server message moves the indicator back
 *   to Listening (without it a silent turn looks like a hung session);
 * - FR-36: the admin drill-down renders a turn the agent chose not to
 *   answer — listed with its outcome and no latency, never missing.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fmtOutcome, modeForServerMessage } from "@/lib/turnOutcome";

afterEach(cleanup);

vi.mock("next/navigation", () => ({ useParams: () => ({ id: "s1" }) }));

const detail = {
  session_id: "s1",
  user_id: "uid-a",
  user_email: "a@example.com",
  started_at: "2026-11-14T10:00:00+00:00",
  ended_at: "2026-11-14T10:05:00+00:00",
  status: "ended",
  end_reason: "user",
  duration_s: 300,
  usage: { "llm.tokens_in": 400, "check.count": 6 },
  estimated_cost: { stt: 0, llm: 0, tts: 0, total: 0, configured: false },
  turns: [
    {
      turn_id: 1,
      eot_to_first_audio_ms: 1400,
      stages_ms: { check: 120, "step.1.ttfb.llm": 900 },
      usage: { "llm.tokens_in": 400, "check.count": 3 },
      estimated_cost: { stt: 0, llm: 0, tts: 0, total: 0, configured: false },
      outcome: "edit_and_speak",
    },
    {
      turn_id: 2,
      eot_to_first_audio_ms: null,
      stages_ms: null,
      usage: { "check.count": 3 },
      estimated_cost: { stt: 0, llm: 0, tts: 0, total: 0, configured: false },
      outcome: "silent_mid_thought",
    },
  ],
};

vi.mock("@/lib/auth", () => ({
  authedFetch: vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => detail,
  })),
}));

// The shell pulls in auth and routing; the page under test needs only its
// formatters and a pass-through frame.
vi.mock("@/app/admin/shell", () => ({
  AdminShell: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  useAdminReady: () => true,
  fmtCost: () => "$0.00",
  fmtDuration: () => "5m",
  fmtInt: (n: number) => String(n),
  fmtWhen: () => "today",
}));

describe("the indicator after a silent turn (FR-64)", () => {
  it("returns to listening on turn.silent", () => {
    expect(modeForServerMessage("turn.silent")).toBe("listening");
  });

  it("is not moved by any other server message", () => {
    for (const type of ["document.created", "session.ending", undefined, 7]) {
      expect(modeForServerMessage(type)).toBeNull();
    }
  });
});

describe("the outcome label (FR-70 vocabulary)", () => {
  it("names each outcome and joins bindings", () => {
    expect(fmtOutcome("silent_mid_thought")).toBe("silent: mid-thought");
    expect(fmtOutcome("silent_nothing_asked")).toBe("silent: nothing asked");
    expect(fmtOutcome("affirm")).toBe("affirm");
    // a part outside the vocabulary passes through as written
    expect(fmtOutcome("talk_only+affirm")).toBe("talk_only, affirm");
    expect(fmtOutcome("answer")).toBe("answer");
    expect(fmtOutcome("edit_and_speak")).toBe("edit and speak");
    expect(fmtOutcome("plain")).toBe("plain");
  });

  it("says when a check went unanswered or was cut off", () => {
    expect(fmtOutcome("plain+bypass")).toBe("plain (check unanswered)");
    expect(fmtOutcome("interrupted")).toBe("check interrupted");
  });

  it("shows a dash for a turn that was never checked, and passes unknowns through", () => {
    expect(fmtOutcome(null)).toBe("—");
    expect(fmtOutcome(undefined)).toBe("—");
    expect(fmtOutcome("something_new")).toBe("something_new");
  });
});

describe("the admin drill-down (FR-36)", () => {
  it("lists a silent turn with its outcome and no latency", async () => {
    const { default: AdminSessionPage } = await import(
      "@/app/admin/sessions/[id]/page"
    );
    render(<AdminSessionPage />);
    const silent = (await screen.findByText("silent: mid-thought")).closest(
      "tr",
    )!;
    const cells = within(silent).getAllByRole("cell");
    expect(cells[0].textContent).toBe("2");
    expect(cells[2].textContent).toBe("—"); // speech → first audio
    // and the answered turn shows its outcome beside its latency
    const spoken = screen.getByText("edit and speak").closest("tr")!;
    expect(within(spoken).getAllByRole("cell")[2].textContent).toBe("1400ms");
  });
});
