export interface SessionMessage {
  role: "user" | "agent";
  text: string;
}

export type AgentStatus =
  | "connecting"
  | "idle"
  | "recording"
  | "processing"
  | "speaking"
  | "error";

interface SessionOptions {
  wsUrl: string;
  onMessage: (msg: SessionMessage) => void;
  onStatusChange: (status: AgentStatus, error?: string) => void;
  onClose: () => void;
}

// Gemini Live expects 16kHz PCM input; returns 24kHz PCM audio output
const INPUT_SAMPLE_RATE = 16000;
const OUTPUT_SAMPLE_RATE = 24000;

export class Session {
  private ws: WebSocket | null = null;
  private audioContext: AudioContext | null = null;
  private mediaStream: MediaStream | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private workletLoaded = false;
  private pcmChunks: ArrayBuffer[] = [];
  private playbackQueue: AudioBuffer[] = [];
  private isPlaying = false;
  private turnComplete = false;
  private hasReceivedAudio = false;
  private opts: SessionOptions;

  constructor(opts: SessionOptions) {
    this.opts = opts;
  }

  // Opens the WebSocket and connects to Gemini Live. Does NOT start the mic.
  async start() {
    this.opts.onStatusChange("connecting");

    this.ws = new WebSocket(`${this.opts.wsUrl}/ws/session`);
    this.ws.binaryType = "arraybuffer";

    await new Promise<void>((resolve, reject) => {
      this.ws!.onopen = () => resolve();
      this.ws!.onerror = () => reject(new Error("WebSocket failed to connect"));
    });

    this.ws.onmessage = (e) => this.handleMessage(e);
    this.ws.onclose = () => this.opts.onClose();

    // AudioContext lives for the full session lifetime (playback uses it between turns)
    this.audioContext = new AudioContext({ sampleRate: INPUT_SAMPLE_RATE });

    this.opts.onStatusChange("idle");
  }

  // Starts mic capture for the current turn. Safe to call multiple times across turns.
  startRecording() {
    this.pcmChunks = [];
    this.turnComplete = false;
    this.hasReceivedAudio = false;

    this._startMic().catch((e) => {
      console.error("[session] mic error:", e);
      this.opts.onStatusChange("error", "Microphone access failed");
    });
  }

  private async _startMic() {
    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: true,
      video: false,
    });

    if (!this.workletLoaded) {
      await this.audioContext!.audioWorklet.addModule("/audio-processor.js");
      this.workletLoaded = true;
    }

    const source = this.audioContext!.createMediaStreamSource(this.mediaStream);
    this.workletNode = new AudioWorkletNode(this.audioContext!, "audio-processor");

    // Buffer PCM chunks locally — don't stream to backend until Stop is pressed
    this.workletNode.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
      this.pcmChunks.push(e.data);
    };

    source.connect(this.workletNode);
    // Muted gain keeps AudioContext alive without feeding mic back to speakers (avoids echo loop)
    const silentGain = this.audioContext!.createGain();
    silentGain.gain.value = 0;
    this.workletNode.connect(silentGain);
    silentGain.connect(this.audioContext!.destination);

    this.opts.onStatusChange("recording");
  }

  // Stops the mic, sends all buffered audio, and signals end of turn.
  // The WebSocket stays open to receive the agent's response.
  stopRecording() {
    this.workletNode?.disconnect();
    this.mediaStream?.getTracks().forEach((t) => t.stop());
    this.workletNode = null;
    this.mediaStream = null;

    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;

    const totalBytes = this.pcmChunks.reduce((sum, b) => sum + b.byteLength, 0);
    if (totalBytes === 0) {
      // Nothing recorded — go back to idle without bothering Gemini
      this.opts.onStatusChange("idle");
      return;
    }

    const combined = new Uint8Array(totalBytes);
    let offset = 0;
    for (const chunk of this.pcmChunks) {
      combined.set(new Uint8Array(chunk), offset);
      offset += chunk.byteLength;
    }
    this.pcmChunks = [];

    console.log(`[session] sending ${totalBytes}B of audio`);
    this.ws.send(combined.buffer);
    this.ws.send(JSON.stringify({ type: "end_of_turn" }));

    this.opts.onStatusChange("processing");
  }

  // Explicitly ends the session and closes the WebSocket.
  endSession() {
    this.close();
  }

  private close() {
    this.workletNode?.disconnect();
    this.mediaStream?.getTracks().forEach((t) => t.stop());
    this.audioContext?.close();
    this.ws?.close();
  }

  private handleMessage(event: MessageEvent) {
    if (event.data instanceof ArrayBuffer) {
      if (!this.hasReceivedAudio) {
        this.hasReceivedAudio = true;
        this.opts.onStatusChange("speaking");
      }
      this.enqueueAudio(event.data);
    } else {
      try {
        const msg = JSON.parse(event.data as string);
        if (msg.type === "transcript") {
          this.opts.onMessage({ role: msg.role, text: msg.text });
        } else if (msg.type === "turn_complete") {
          this.turnComplete = true;
          // No audio at all — empty response. Go back to idle so the user can speak again.
          if (!this.isPlaying && this.playbackQueue.length === 0) {
            this.opts.onStatusChange("idle");
          }
          // If audio is playing, playNext() will fire idle when the queue drains.
        } else if (msg.type === "error") {
          this.opts.onStatusChange("error", msg.message ?? "Agent error");
        }
      } catch {
        // ignore malformed JSON frames
      }
    }
  }

  private enqueueAudio(pcmBuffer: ArrayBuffer) {
    if (!this.audioContext) return;

    const int16 = new Int16Array(pcmBuffer);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      float32[i] = int16[i] / 32768;
    }

    const buffer = this.audioContext.createBuffer(1, float32.length, OUTPUT_SAMPLE_RATE);
    buffer.copyToChannel(float32, 0);
    this.playbackQueue.push(buffer);

    if (!this.isPlaying) this.playNext();
  }

  private playNext() {
    if (
      !this.audioContext ||
      this.audioContext.state === "closed" ||
      this.playbackQueue.length === 0
    ) {
      this.isPlaying = false;
      // Playback drained — go idle only once turn_complete has been received
      if (this.turnComplete) {
        this.opts.onStatusChange("idle");
      }
      // If turn_complete hasn't arrived yet, handleMessage will trigger idle when it does
      return;
    }

    this.isPlaying = true;
    const buffer = this.playbackQueue.shift()!;
    const source = this.audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(this.audioContext.destination);
    source.onended = () => this.playNext();
    source.start();
  }
}
