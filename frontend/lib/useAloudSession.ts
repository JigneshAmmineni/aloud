"use client";

/**
 * Session state machine + Pipecat client wiring (SDD §2.1).
 *
 * Button FSM (FR-5):  idle → connecting → active → ending → idle
 * Voice mode (FR-6):  listening | thinking | speaking — only meaningful
 *                     while the session is active.
 */

import { useCallback, useRef, useState } from "react";
import { PipecatClient, type TransportState } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

export type SessionState = "idle" | "connecting" | "active" | "ending";
export type VoiceMode = "listening" | "thinking" | "speaking";

export type Artifact = {
  id: number;
  title: string;
  kind: string;
  content: string;
  created_at: string;
};

export type AttachedDocument = {
  id: string;
  filename: string;
  char_count: number;
};

export function useAloudSession() {
  const [state, setState] = useState<SessionState>("idle");
  const [mode, setMode] = useState<VoiceMode>("listening");
  const [error, setError] = useState<string | null>(null);
  const [localTrack, setLocalTrack] = useState<MediaStreamTrack | null>(null);
  const [botTrack, setBotTrack] = useState<MediaStreamTrack | null>(null);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [documents, setDocuments] = useState<AttachedDocument[]>([]);

  const clientRef = useRef<PipecatClient | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const cleanup = useCallback(() => {
    clientRef.current = null;
    setLocalTrack(null);
    setBotTrack(null);
    setMode("listening");
    setState("idle");
  }, []);

  // Upload a file to the backend; on success it's attached to the next
  // session. Throws with the backend's message so the caller can show it.
  const uploadDocument = useCallback(async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch("/documents", { method: "POST", body: form });
    if (!res.ok) {
      const detail = await res
        .json()
        .then((d) => d?.detail)
        .catch(() => null);
      throw new Error(detail || "couldn't read that file");
    }
    const doc = (await res.json()) as AttachedDocument;
    setDocuments((prev) => [...prev, doc]);
  }, []);

  const removeDocument = useCallback((id: string) => {
    setDocuments((prev) => prev.filter((d) => d.id !== id));
  }, []);

  const talk = useCallback(async () => {
    if (clientRef.current) return;
    setError(null);
    setState("connecting");

    // Hidden sink for the bot's voice; created on the tap (user gesture) so
    // autoplay is permitted.
    if (!audioRef.current) {
      const el = document.createElement("audio");
      el.autoplay = true;
      document.body.appendChild(el);
      audioRef.current = el;
    }

    try {
      const client = new PipecatClient({
        transport: new SmallWebRTCTransport({
          iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
        }),
        enableMic: true,
        enableCam: false,
        callbacks: {
          onTransportStateChanged: (s: TransportState) => {
            if (s === "ready") setState("active");
            if (s === "disconnected" || s === "error") cleanup();
          },
          // FR-6 waveform modes, driven by server-side speech events.
          onBotStartedSpeaking: () => setMode("speaking"),
          onBotStoppedSpeaking: () => setMode("listening"),
          onUserStartedSpeaking: () => setMode("listening"),
          onUserStoppedSpeaking: () => setMode("thinking"),
          onTrackStarted: (track, participant) => {
            if (track.kind !== "audio") return;
            if (participant?.local) {
              setLocalTrack(track);
            } else {
              setBotTrack(track);
              if (audioRef.current) {
                audioRef.current.srcObject = new MediaStream([track]);
              }
            }
          },
          // FR-12: the create_artifact tool announces new artifacts here.
          onServerMessage: (data: any) => {
            if (data?.type === "artifact.created" && data.artifact) {
              setArtifacts((prev) => [data.artifact as Artifact, ...prev]);
            }
          },
        },
      });
      clientRef.current = client;
      if (process.env.NODE_ENV !== "production") {
        // dev hook for driving the client from the console / E2E tests
        (window as unknown as Record<string, unknown>).__aloudClient = client;
      }
      // Bootstrap a session so any attached documents reach the agent, then
      // connect on the session-scoped offer path the backend serves them from.
      const startRes = await fetch("/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          body: { document_ids: documents.map((d) => d.id) },
        }),
      });
      if (!startRes.ok) throw new Error("session start failed");
      const { sessionId } = (await startRes.json()) as { sessionId: string };
      await client.connect({ webrtcUrl: `/sessions/${sessionId}/api/offer` });
    } catch (e) {
      console.error("connect failed", e);
      cleanup();
      setError("couldn't connect — check that the backend is running");
    }
  }, [cleanup, documents]);

  const end = useCallback(async () => {
    const client = clientRef.current;
    if (!client) return;
    setState("ending");
    try {
      await client.disconnect();
    } catch {
      // disconnect errors don't matter; the session is over either way
    }
    cleanup();
  }, [cleanup]);

  return {
    state,
    mode,
    error,
    localTrack,
    botTrack,
    artifacts,
    documents,
    uploadDocument,
    removeDocument,
    talk,
    end,
  };
}
