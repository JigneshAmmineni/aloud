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

import { authedFetch, getToken } from "@/lib/auth";

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

const LOST_MESSAGE =
  "connection lost — that session has ended. tap Talk to start a new one.";
const RESTART_MESSAGE =
  "the server restarted — that session has ended. tap Talk to start a new one.";

// Session-liveness poll (unexpected-death detection): cadence and how many
// consecutive poll failures mean the backend is gone. An authoritative
// "alive: false" answer ends the session immediately, without waiting.
const ALIVE_POLL_MS = 5_000;
const ALIVE_MAX_MISSES = 2;

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
  const sessionIdRef = useRef<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollMissesRef = useRef(0);
  // alive:false only becomes authoritative after the session has been seen
  // alive once — the DB row is created by a background task that can lag
  // the transport's `ready`, and "not created yet" must not read as "dead".
  const sawAliveRef = useRef(false);
  // True while a disconnect is expected (user tapped End, or the server said
  // goodbye) — distinguishes it from an unexpected drop, which gets a notice.
  const expectedEndRef = useRef(false);

  const cleanup = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
    pollMissesRef.current = 0;
    sawAliveRef.current = false;
    sessionIdRef.current = null;
    clientRef.current = null;
    setLocalTrack(null);
    setBotTrack(null);
    setMode("listening");
    setState("idle");
  }, []);

  // Unexpected session death: tear down (disconnect releases the mic; it may
  // fail against a dead backend, which is fine) and leave a persistent
  // notice. Artifacts already on screen are deliberately kept.
  const sessionLost = useCallback(
    (message: string) => {
      expectedEndRef.current = true; // the transport close that follows is expected
      const client = clientRef.current;
      if (client) client.disconnect().catch(() => {});
      cleanup();
      setError(message);
    },
    [cleanup],
  );

  // While active, poll session-scoped liveness — the DB-backed answer covers
  // every death the transport is slow to notice (backend crash/restart,
  // media-path timeout the server ended). See /sessions/{id}/alive.
  const startAlivePoll = useCallback(() => {
    if (pollRef.current) return;
    pollMissesRef.current = 0;
    pollRef.current = setInterval(async () => {
      const sessionId = sessionIdRef.current;
      if (!sessionId || !clientRef.current) return;
      try {
        const res = await authedFetch(`/sessions/${sessionId}/alive`);
        if (res.ok) {
          const { alive } = (await res.json()) as { alive: boolean };
          if (alive) {
            sawAliveRef.current = true;
            pollMissesRef.current = 0;
            return;
          }
          if (sawAliveRef.current) {
            sessionLost(LOST_MESSAGE); // authoritative: it lived, now it's over
            return;
          }
          pollMissesRef.current += 1; // row may not exist yet: just a miss
        } else {
          pollMissesRef.current += 1;
        }
      } catch {
        pollMissesRef.current += 1;
      }
      if (pollMissesRef.current >= ALIVE_MAX_MISSES) sessionLost(LOST_MESSAGE);
    }, ALIVE_POLL_MS);
  }, [sessionLost]);

  // Upload a file to the backend; on success it's attached to the next
  // session. Throws with the backend's message so the caller can show it.
  const uploadDocument = useCallback(async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await authedFetch("/documents", { method: "POST", body: form });
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
    expectedEndRef.current = false;
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
            if (s === "ready") {
              setState("active");
              startAlivePoll();
            }
            if (s === "disconnected" || s === "error") {
              // Expected after End or a server goodbye; anything else is an
              // unexpected drop the user must be told about.
              if (expectedEndRef.current) cleanup();
              else sessionLost(LOST_MESSAGE);
            }
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
            // Graceful-shutdown goodbye: the server says it's going away
            // (deploy/restart) before the connection drops.
            if (data?.type === "session.ending") {
              sessionLost(RESTART_MESSAGE);
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
      const startRes = await authedFetch("/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          body: { document_ids: documents.map((d) => d.id) },
        }),
      });
      if (!startRes.ok) throw new Error("session start failed");
      const { sessionId } = (await startRes.json()) as { sessionId: string };
      sessionIdRef.current = sessionId;
      // The offer + trickle-ICE requests must carry the Bearer token too
      // (FR-23/FR-29) — handing the transport a Request bakes the header
      // into every signaling call it derives from it.
      const token = await getToken();
      await client.connect({
        webrtcRequestParams: {
          endpoint: new Request(`/sessions/${sessionId}/api/offer`, {
            method: "POST",
            headers: {
              // With an endpoint Request, the transport uses these headers
              // verbatim (no defaults) — Content-Type must come along or the
              // JSON body arrives unparseable (422).
              "Content-Type": "application/json",
              Authorization: `Bearer ${token}`,
            },
          }),
        },
      });
    } catch (e) {
      console.error("connect failed", e);
      expectedEndRef.current = true; // a failed connect is not a lost session
      cleanup();
      // User-appropriate wording only: the person on the phone can't "check
      // the backend" — retrying is the one action they actually have.
      setError("couldn't connect — try again shortly.");
    }
  }, [cleanup, documents, sessionLost, startAlivePoll]);

  const end = useCallback(async () => {
    const client = clientRef.current;
    if (!client) return;
    expectedEndRef.current = true;
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
