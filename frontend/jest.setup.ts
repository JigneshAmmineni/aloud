import "@testing-library/jest-dom";

// ---------------------------------------------------------------------------
// WebSocket mock
// ---------------------------------------------------------------------------

type WSSentItem = string | ArrayBuffer | Blob;

class MockWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  readyState = MockWebSocket.CONNECTING;
  binaryType = "blob";
  sent: WSSentItem[] = [];

  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;

  constructor(public url: string) {
    MockWebSocket._instances.push(this);
    // Do NOT auto-open. Tests control connection success via ws._open() / ws.onerror().
    // The startSession helper calls _open() explicitly.
  }

  _open() {
    this.readyState = MockWebSocket.OPEN;
    this.onopen?.();
  }

  _close() {
    this.readyState = MockWebSocket.CLOSED;
    this.onclose?.();
  }

  _message(data: string | ArrayBuffer) {
    this.onmessage?.({ data } as MessageEvent);
  }

  send(data: WSSentItem) {
    this.sent.push(data);
  }

  close() {
    this.readyState = MockWebSocket.CLOSING;
    Promise.resolve().then(() => this._close());
  }

  static _instances: MockWebSocket[] = [];

  static _reset() {
    MockWebSocket._instances = [];
  }

  static _latest(): MockWebSocket {
    return MockWebSocket._instances[MockWebSocket._instances.length - 1];
  }
}

(global as unknown as Record<string, unknown>).WebSocket = MockWebSocket;

// ---------------------------------------------------------------------------
// AudioContext mock
// ---------------------------------------------------------------------------

class MockAudioBufferSource {
  buffer: AudioBuffer | null = null;
  onended: (() => void) | null = null;
  connected = false;

  connect() {
    this.connected = true;
    return this;
  }

  start() {
    // Auto-fire onended on next tick for playback drain tests
    Promise.resolve().then(() => this.onended?.());
  }
}

class MockAudioBuffer {
  constructor(
    public numberOfChannels: number,
    public length: number,
    public sampleRate: number
  ) {}

  copyToChannel(_data: Float32Array, _channel: number) {}
}

class MockGain {
  gain = { value: 0 };
  connect() {
    return this;
  }
}

class MockAudioWorklet {
  async addModule(_url: string) {}
}

class MockAudioWorkletNode {
  port = {
    onmessage: null as ((e: MessageEvent) => void) | null,
    postMessage(_data: unknown) {},
  };

  connect() {
    return this;
  }

  disconnect() {}
}

class MockMediaStreamSource {
  connect() {
    return this;
  }
}

class MockAudioContext {
  state: "running" | "closed" = "running";
  destination = {};
  audioWorklet = new MockAudioWorklet();

  createBuffer(channels: number, length: number, rate: number) {
    return new MockAudioBuffer(channels, length, rate);
  }

  createBufferSource() {
    return new MockAudioBufferSource();
  }

  createGain() {
    return new MockGain();
  }

  createMediaStreamSource(_stream: MediaStream) {
    return new MockMediaStreamSource();
  }

  close() {
    this.state = "closed";
  }
}

(global as unknown as Record<string, unknown>).AudioContext = MockAudioContext;
(global as unknown as Record<string, unknown>).AudioWorkletNode =
  MockAudioWorkletNode;

// ---------------------------------------------------------------------------
// MediaDevices / getUserMedia mock
// ---------------------------------------------------------------------------

const mockTrack = { stop: jest.fn(), kind: "audio" };
const mockStream = {
  getTracks: () => [mockTrack],
  getAudioTracks: () => [mockTrack],
};

Object.defineProperty(global.navigator, "mediaDevices", {
  value: {
    getUserMedia: jest.fn().mockResolvedValue(mockStream),
  },
  writable: true,
  configurable: true,
});

// ---------------------------------------------------------------------------
// Expose helpers so individual tests can reset state
// ---------------------------------------------------------------------------

(global as unknown as Record<string, unknown>).MockWebSocket = MockWebSocket;

afterEach(() => {
  MockWebSocket._reset();
  jest.clearAllMocks();
});
