export interface SessionMessage {
  role: "user" | "agent";
  text: string;
}

interface SessionOptions {
  wsUrl: string;
  onMessage: (msg: SessionMessage) => void;
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
  private playbackQueue: AudioBuffer[] = [];
  private isPlaying = false;
  private opts: SessionOptions;

  constructor(opts: SessionOptions) {
    this.opts = opts;
  }

  async start() {
    this.ws = new WebSocket(`${this.opts.wsUrl}/ws/session`);
    this.ws.binaryType = "arraybuffer";

    await new Promise<void>((resolve, reject) => {
      this.ws!.onopen = () => resolve();
      this.ws!.onerror = () => reject(new Error("WebSocket failed to connect"));
    });

    this.ws.onmessage = (e) => this.handleMessage(e);
    this.ws.onclose = () => this.opts.onClose();

    await this.startMic();
  }

  private async startMic() {
    this.audioContext = new AudioContext({ sampleRate: INPUT_SAMPLE_RATE });
    this.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });

    await this.audioContext.audioWorklet.addModule("/audio-processor.js");

    const source = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.workletNode = new AudioWorkletNode(this.audioContext, "audio-processor");

    this.workletNode.port.onmessage = (e: MessageEvent<ArrayBuffer>) => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(e.data);
      }
    };

    source.connect(this.workletNode);
    // Connect to destination so AudioContext stays active (required by some browsers)
    this.workletNode.connect(this.audioContext.destination);
  }

  private handleMessage(event: MessageEvent) {
    if (event.data instanceof ArrayBuffer) {
      this.enqueueAudio(event.data);
    } else {
      try {
        const msg = JSON.parse(event.data as string);
        if (msg.type === "transcript") {
          this.opts.onMessage({ role: msg.role, text: msg.text });
        }
      } catch {
        // ignore malformed messages
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
    if (!this.audioContext || this.playbackQueue.length === 0) {
      this.isPlaying = false;
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

  stop() {
    this.workletNode?.disconnect();
    this.mediaStream?.getTracks().forEach((t) => t.stop());
    this.audioContext?.close();
    this.ws?.close();
  }
}
