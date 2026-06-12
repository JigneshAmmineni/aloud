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

export function useAloudSession() {
  const [state, setState] = useState<SessionState>("idle");
  const [mode, setMode] = useState<VoiceMode>("listening");
  const [error, setError] = useState<string | null>(null);
  const [localTrack, setLocalTrack] = useState<MediaStreamTrack | null>(null);
  const [botTrack, setBotTrack] = useState<MediaStreamTrack | null>(null);

  const clientRef = useRef<PipecatClient | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const cleanup = useCallback(() => {
    clientRef.current = null;
    setLocalTrack(null);
    setBotTrack(null);
    setMode("listening");
    setState("idle");
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
        },
      });
      clientRef.current = client;
      await client.connect({ webrtcUrl: "/api/offer" });
    } catch (e) {
      console.error("connect failed", e);
      cleanup();
      setError("couldn't connect — check that the backend is running");
    }
  }, [cleanup]);

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

  return { state, mode, error, localTrack, botTrack, talk, end };
}
