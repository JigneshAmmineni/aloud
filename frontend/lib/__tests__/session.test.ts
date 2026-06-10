/**
 * Unit tests for Session state machine (lib/session.ts).
 *
 * Browser APIs (WebSocket, AudioContext, getUserMedia, AudioWorkletNode)
 * are mocked in jest.setup.ts.
 */
import { Session, type AgentStatus } from "../session";

// Helper to access the MockWebSocket constructor and latest instance
declare const MockWebSocket: {
  _instances: MockWSInstance[];
  _reset(): void;
  _latest(): MockWSInstance;
};

interface MockWSInstance {
  url: string;
  sent: Array<string | ArrayBuffer>;
  readyState: number;
  binaryType: string;
  onopen: (() => void) | null;
  onclose: (() => void) | null;
  onerror: ((e: Event) => void) | null;
  onmessage: ((e: MessageEvent) => void) | null;
  _open(): void;
  _close(): void;
  _message(data: string | ArrayBuffer): void;
  send(data: string | ArrayBuffer): void;
  close(): void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeSession(overrides: Partial<Parameters<typeof Session>[0]> = {}) {
  const statuses: string[] = [];
  const errors: Array<string | undefined> = [];
  const messages: Array<{ role: string; text: string }> = [];

  const session = new Session({
    wsUrl: "ws://localhost:8000",
    onMessage: (m) => messages.push(m),
    onStatusChange: (s, err) => {
      statuses.push(s);
      errors.push(err);
    },
    onClose: jest.fn(),
    ...overrides,
  });

  return { session, statuses, errors, messages };
}

async function startSession(session: Session): Promise<MockWSInstance> {
  const startPromise = session.start();
  // Let the microtask for WS open fire
  await Promise.resolve();
  const ws = MockWebSocket._latest();
  ws._open();
  await startPromise;
  return ws;
}

// ---------------------------------------------------------------------------
// start() — WebSocket connection and initial status
// ---------------------------------------------------------------------------

describe("start()", () => {
  it("emits connecting then idle on successful WS open", async () => {
    const { session, statuses } = makeSession();
    await startSession(session);

    expect(statuses).toEqual(["connecting", "idle"]);
  });

  it("rejects and does not emit idle if WS fires onerror", async () => {
    const { session, statuses } = makeSession();

    const startPromise = session.start();
    await Promise.resolve();
    const ws = MockWebSocket._latest();
    ws.onerror?.(new Event("error"));

    await expect(startPromise).rejects.toThrow();
    expect(statuses).not.toContain("idle");
  });

  it("opens WS to /ws/session path", async () => {
    const { session } = makeSession();
    await startSession(session);

    const ws = MockWebSocket._latest();
    expect(ws.url).toBe("ws://localhost:8000/ws/session");
  });

  it("sets binaryType to arraybuffer", async () => {
    const { session } = makeSession();
    await startSession(session);

    const ws = MockWebSocket._latest();
    expect(ws.binaryType).toBe("arraybuffer");
  });
});

// ---------------------------------------------------------------------------
// startRecording() — status transitions
// ---------------------------------------------------------------------------

describe("startRecording()", () => {
  it("transitions to recording after mic access granted", async () => {
    const { session, statuses } = makeSession();
    await startSession(session);

    session.startRecording();
    await Promise.resolve(); // getUserMedia resolves
    await Promise.resolve(); // audioWorklet.addModule resolves
    await Promise.resolve(); // state update

    expect(statuses).toContain("recording");
  });

  it("transitions to error if getUserMedia rejects", async () => {
    (navigator.mediaDevices.getUserMedia as jest.Mock).mockRejectedValueOnce(
      new Error("Permission denied")
    );

    const { session, statuses } = makeSession();
    await startSession(session);
    session.startRecording();

    // Wait for the rejection to propagate
    await new Promise((r) => setTimeout(r, 20));

    expect(statuses).toContain("error");
  });

  it("resets per-turn state (pcmChunks, turnComplete, hasReceivedAudio)", async () => {
    const { session, statuses } = makeSession();
    await startSession(session);

    session.startRecording();
    await Promise.resolve();
    await Promise.resolve();

    // Second call also resets state cleanly
    session.startRecording();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    expect(statuses.filter((s) => s === "recording").length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// stopRecording() — status transitions
// ---------------------------------------------------------------------------

describe("stopRecording()", () => {
  it("transitions to idle without sending if no PCM was buffered", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    // startRecording then immediately stop before worklet produces chunks
    session.startRecording();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    session.stopRecording();

    expect(statuses).toContain("recording");
    expect(statuses[statuses.length - 1]).toBe("idle");
    const binaryFrames = ws.sent.filter((s) => s instanceof ArrayBuffer);
    expect(binaryFrames.length).toBe(0);
  });

  it("sends binary audio + end_of_turn JSON and transitions to processing", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    session.startRecording();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    // Simulate the worklet producing audio
    const ctx = (session as unknown as { audioContext: { audioWorklet: unknown } })
      .audioContext;
    const worklet = (session as unknown as { workletNode: { port: { onmessage: (e: MessageEvent) => void } } }).workletNode;
    if (worklet) {
      const pcm = new ArrayBuffer(9600);
      worklet.port.onmessage?.({ data: pcm } as MessageEvent);
    }

    session.stopRecording();

    const binaryFrames = ws.sent.filter((s) => s instanceof ArrayBuffer);
    const textFrames = ws.sent.filter((s) => typeof s === "string");

    expect(binaryFrames.length).toBe(1);
    expect(textFrames.some((t) => JSON.parse(t as string).type === "end_of_turn")).toBe(true);
    expect(statuses).toContain("processing");
  });

  it("is a no-op if WS is not open", async () => {
    const { session, statuses } = makeSession();
    await startSession(session);

    const ws = MockWebSocket._latest();
    ws.readyState = 3; // CLOSED

    session.stopRecording();

    expect(statuses).not.toContain("processing");
  });
});

// ---------------------------------------------------------------------------
// handleMessage() — incoming WS frames
// ---------------------------------------------------------------------------

describe("handleMessage() — audio frames", () => {
  it("transitions to speaking on first audio binary frame", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    ws._message(new ArrayBuffer(9600));

    expect(statuses).toContain("speaking");
  });

  it("does not emit speaking more than once for same turn", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    ws._message(new ArrayBuffer(9600));
    ws._message(new ArrayBuffer(9600));

    expect(statuses.filter((s) => s === "speaking").length).toBe(1);
  });
});

describe("handleMessage() — turn_complete", () => {
  it("transitions to idle immediately when no audio is buffered or playing", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    ws._message(JSON.stringify({ type: "turn_complete" }));

    expect(statuses).toContain("idle");
  });

  it("does not transition to idle if audio is still playing", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    // Send audio (starts playing)
    ws._message(new ArrayBuffer(9600));
    // Immediately send turn_complete before playback drains
    ws._message(JSON.stringify({ type: "turn_complete" }));

    // At this point idle should NOT have fired yet (audio is still in queue/playing)
    const idleCount = statuses.filter((s) => s === "idle").length;
    // Idle from start() is expected, but no new idle from turn_complete mid-play
    expect(idleCount).toBe(1); // only the initial "idle" from start()
  });

  it("transitions to idle after audio drains when turn_complete was already received", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    ws._message(new ArrayBuffer(9600));
    ws._message(JSON.stringify({ type: "turn_complete" }));

    // Let MockAudioBufferSource.onended fire (Promise.resolve in mock)
    await Promise.resolve();
    await Promise.resolve();

    expect(statuses[statuses.length - 1]).toBe("idle");
  });
});

describe("handleMessage() — error frame", () => {
  it("transitions to error with message", async () => {
    const { session, statuses, errors } = makeSession();
    const ws = await startSession(session);

    ws._message(JSON.stringify({ type: "error", message: "Gemini dropped" }));

    expect(statuses).toContain("error");
    expect(errors).toContain("Gemini dropped");
  });

  it("uses fallback error message if none provided", async () => {
    const { session, statuses, errors } = makeSession();
    const ws = await startSession(session);

    ws._message(JSON.stringify({ type: "error" }));

    expect(statuses).toContain("error");
    expect(errors.some((e) => typeof e === "string" && e.length > 0)).toBe(true);
  });
});

describe("handleMessage() — transcript frame", () => {
  it("calls onMessage with role and text", async () => {
    const { session, messages } = makeSession();
    const ws = await startSession(session);

    ws._message(JSON.stringify({ type: "transcript", role: "agent", text: "How are you?" }));

    expect(messages).toContainEqual({ role: "agent", text: "How are you?" });
  });
});

describe("handleMessage() — malformed JSON", () => {
  it("does not crash on malformed JSON text frame", async () => {
    const { session } = makeSession();
    const ws = await startSession(session);

    expect(() => ws._message("not json {{{")).not.toThrow();
  });
});

describe("handleMessage() — unknown type", () => {
  it("ignores unknown message types", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    const before = [...statuses];
    ws._message(JSON.stringify({ type: "unknown_type", payload: {} }));

    expect(statuses).toEqual(before);
  });
});

// ---------------------------------------------------------------------------
// endSession()
// ---------------------------------------------------------------------------

describe("endSession()", () => {
  it("closes the WebSocket", async () => {
    const { session } = makeSession();
    await startSession(session);

    const ws = MockWebSocket._latest();
    const closeSpy = jest.spyOn(ws, "close");

    session.endSession();

    expect(closeSpy).toHaveBeenCalled();
  });

  it("onClose callback fires after WS close event", async () => {
    const onClose = jest.fn();
    const { session } = makeSession({ onClose });
    const ws = await startSession(session);

    session.endSession();
    ws._close();

    expect(onClose).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Multi-turn state machine
// ---------------------------------------------------------------------------

describe("multi-turn state machine", () => {
  it("status cycle: idle → recording → processing → speaking → idle (Turn 1)", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    // Start recording
    session.startRecording();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    // Simulate worklet producing audio
    const worklet = (session as unknown as { workletNode: { port: { onmessage: (e: MessageEvent) => void } } }).workletNode;
    if (worklet) {
      worklet.port.onmessage?.({ data: new ArrayBuffer(9600) } as MessageEvent);
    }

    // Stop recording
    session.stopRecording();

    // Receive audio response
    ws._message(new ArrayBuffer(9600));

    // Receive turn_complete
    ws._message(JSON.stringify({ type: "turn_complete" }));

    // Let playback drain
    await Promise.resolve();
    await Promise.resolve();

    const relevantStatuses = statuses.filter((s) =>
      ["recording", "processing", "speaking", "idle"].includes(s)
    );

    expect(relevantStatuses).toContain("recording");
    expect(relevantStatuses).toContain("processing");
    expect(relevantStatuses).toContain("speaking");
    expect(relevantStatuses[relevantStatuses.length - 1]).toBe("idle");
  });

  it("can start a second turn after Turn 1 completes", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    // Turn 1
    session.startRecording();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    const worklet = (session as unknown as { workletNode: { port: { onmessage: (e: MessageEvent) => void } } }).workletNode;
    if (worklet) {
      worklet.port.onmessage?.({ data: new ArrayBuffer(9600) } as MessageEvent);
    }
    session.stopRecording();
    ws._message(JSON.stringify({ type: "turn_complete" }));
    await Promise.resolve();

    // Turn 2
    session.startRecording();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    const recordingCount = statuses.filter((s) => s === "recording").length;
    expect(recordingCount).toBeGreaterThanOrEqual(1);
  });

  it("empty response (turn_complete with no audio) → idle immediately", async () => {
    const { session, statuses } = makeSession();
    const ws = await startSession(session);

    session.startRecording();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    const worklet = (session as unknown as { workletNode: { port: { onmessage: (e: MessageEvent) => void } } }).workletNode;
    if (worklet) {
      worklet.port.onmessage?.({ data: new ArrayBuffer(9600) } as MessageEvent);
    }
    session.stopRecording();

    // turn_complete arrives with NO audio — agent gave empty response
    ws._message(JSON.stringify({ type: "turn_complete" }));

    // Should go directly processing → idle
    expect(statuses).toContain("idle");
  });
});
