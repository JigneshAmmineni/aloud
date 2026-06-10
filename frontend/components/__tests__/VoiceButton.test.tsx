/**
 * Unit tests for VoiceButton component.
 *
 * Tests rendering (label, color, disabled state) and interaction
 * (onSpeak vs onStop routing) for every AgentStatus value.
 */
import { render, screen, fireEvent } from "@testing-library/react";
import VoiceButton from "../VoiceButton";
import type { AgentStatus } from "@/lib/session";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderButton(
  status: AgentStatus | "disconnected",
  onSpeak = jest.fn(),
  onStop = jest.fn()
) {
  return render(<VoiceButton status={status} onSpeak={onSpeak} onStop={onStop} />);
}

// ---------------------------------------------------------------------------
// Label rendering
// ---------------------------------------------------------------------------

describe("label rendering", () => {
  const cases: Array<[AgentStatus | "disconnected", string]> = [
    ["disconnected", "Speak"],
    ["idle", "Speak"],
    ["recording", "Stop"],
    ["connecting", "···"],
    ["processing", "···"],
    ["speaking", "♪"],
    ["error", "Speak"],
  ];

  test.each(cases)("status=%s renders label %s", (status, expectedLabel) => {
    renderButton(status);
    expect(screen.getByRole("button")).toHaveTextContent(expectedLabel);
  });
});

// ---------------------------------------------------------------------------
// Disabled states
// ---------------------------------------------------------------------------

describe("disabled states", () => {
  it("connecting → button is disabled", () => {
    renderButton("connecting");
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("processing → button is disabled", () => {
    renderButton("processing");
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("speaking → button is disabled", () => {
    renderButton("speaking");
    expect(screen.getByRole("button")).toBeDisabled();
  });

  it("disconnected → button is enabled", () => {
    renderButton("disconnected");
    expect(screen.getByRole("button")).not.toBeDisabled();
  });

  it("idle → button is enabled", () => {
    renderButton("idle");
    expect(screen.getByRole("button")).not.toBeDisabled();
  });

  it("recording → button is enabled", () => {
    renderButton("recording");
    expect(screen.getByRole("button")).not.toBeDisabled();
  });

  it("error → button is enabled (allows retry)", () => {
    renderButton("error");
    expect(screen.getByRole("button")).not.toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Click routing — onSpeak vs onStop
// ---------------------------------------------------------------------------

describe("click routing", () => {
  it("disconnected: click calls onSpeak, not onStop", () => {
    const onSpeak = jest.fn();
    const onStop = jest.fn();
    renderButton("disconnected", onSpeak, onStop);

    fireEvent.click(screen.getByRole("button"));

    expect(onSpeak).toHaveBeenCalledTimes(1);
    expect(onStop).not.toHaveBeenCalled();
  });

  it("idle: click calls onSpeak", () => {
    const onSpeak = jest.fn();
    const onStop = jest.fn();
    renderButton("idle", onSpeak, onStop);

    fireEvent.click(screen.getByRole("button"));

    expect(onSpeak).toHaveBeenCalledTimes(1);
    expect(onStop).not.toHaveBeenCalled();
  });

  it("recording: click calls onStop, not onSpeak", () => {
    const onSpeak = jest.fn();
    const onStop = jest.fn();
    renderButton("recording", onSpeak, onStop);

    fireEvent.click(screen.getByRole("button"));

    expect(onStop).toHaveBeenCalledTimes(1);
    expect(onSpeak).not.toHaveBeenCalled();
  });

  it("error: click calls onSpeak (allows session retry)", () => {
    const onSpeak = jest.fn();
    const onStop = jest.fn();
    renderButton("error", onSpeak, onStop);

    fireEvent.click(screen.getByRole("button"));

    expect(onSpeak).toHaveBeenCalledTimes(1);
    expect(onStop).not.toHaveBeenCalled();
  });

  it("connecting: click does nothing (button disabled)", () => {
    const onSpeak = jest.fn();
    const onStop = jest.fn();
    renderButton("connecting", onSpeak, onStop);

    fireEvent.click(screen.getByRole("button"));

    expect(onSpeak).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });

  it("processing: click does nothing (button disabled)", () => {
    const onSpeak = jest.fn();
    const onStop = jest.fn();
    renderButton("processing", onSpeak, onStop);

    fireEvent.click(screen.getByRole("button"));

    expect(onSpeak).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });

  it("speaking: click does nothing (button disabled)", () => {
    const onSpeak = jest.fn();
    const onStop = jest.fn();
    renderButton("speaking", onSpeak, onStop);

    fireEvent.click(screen.getByRole("button"));

    expect(onSpeak).not.toHaveBeenCalled();
    expect(onStop).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Visual state — background color and animation
// ---------------------------------------------------------------------------

describe("background colors", () => {
  // jsdom normalizes hex colors to rgb() format
  it("disconnected + idle → blue (#3182ce)", () => {
    renderButton("disconnected");
    const btn = screen.getByRole("button") as HTMLButtonElement;
    expect(btn.style.background).toBe("rgb(49, 130, 206)");
  });

  it("recording → red (#e53e3e)", () => {
    renderButton("recording");
    const btn = screen.getByRole("button") as HTMLButtonElement;
    expect(btn.style.background).toBe("rgb(229, 62, 62)");
  });

  it("speaking → green (#38a169)", () => {
    renderButton("speaking");
    const btn = screen.getByRole("button") as HTMLButtonElement;
    expect(btn.style.background).toBe("rgb(56, 161, 105)");
  });

  it("connecting + processing → grey (#718096 or #a0aec0)", () => {
    renderButton("processing");
    const btn = screen.getByRole("button") as HTMLButtonElement;
    expect(["rgb(113, 128, 150)", "rgb(160, 174, 192)"]).toContain(btn.style.background);
  });
});

describe("animation", () => {
  it("recording has a pulse animation", () => {
    renderButton("recording");
    const btn = screen.getByRole("button") as HTMLButtonElement;
    expect(btn.style.animation).toMatch(/pulse/);
  });

  it("speaking has a pulse animation", () => {
    renderButton("speaking");
    const btn = screen.getByRole("button") as HTMLButtonElement;
    expect(btn.style.animation).toMatch(/pulse/);
  });

  it("idle has no animation", () => {
    renderButton("idle");
    const btn = screen.getByRole("button") as HTMLButtonElement;
    expect(btn.style.animation).toBe("none");
  });
});

// ---------------------------------------------------------------------------
// Accessibility
// ---------------------------------------------------------------------------

describe("accessibility", () => {
  it("renders a single button element", () => {
    renderButton("idle");
    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("disabled button has aria-disabled or disabled attribute", () => {
    renderButton("connecting");
    const btn = screen.getByRole("button");
    expect(btn).toBeDisabled();
  });
});
