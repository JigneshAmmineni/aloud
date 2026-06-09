"use client";

import { useState, useCallback, useRef } from "react";
import VoiceButton from "@/components/VoiceButton";
import Transcript from "@/components/Transcript";
import { Session, type SessionMessage, type AgentStatus } from "@/lib/session";

const STATUS_LABEL: Record<AgentStatus | "disconnected", string> = {
  disconnected: "",
  connecting: "Connecting...",
  idle: "Ready",
  recording: "Listening...",
  processing: "Thinking...",
  speaking: "Speaking...",
  error: "",
};

export default function Home() {
  const [status, setStatus] = useState<AgentStatus | "disconnected">("disconnected");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const sessionRef = useRef<Session | null>(null);

  const handleSpeak = useCallback(async () => {
    if (!sessionRef.current) {
      const session = new Session({
        wsUrl: process.env.NEXT_PUBLIC_BACKEND_WS_URL ?? "ws://localhost:8000",
        onMessage: (msg) => setMessages((prev) => [...prev, msg]),
        onStatusChange: (s, err) => {
          setStatus(s);
          setErrorMsg(s === "error" ? (err ?? "Something went wrong") : null);
        },
        onClose: () => {
          setStatus("disconnected");
          setErrorMsg(null);
          sessionRef.current = null;
        },
      });
      sessionRef.current = session;
      try {
        await session.start();
      } catch (e) {
        console.error("Failed to connect:", e);
        sessionRef.current = null;
        setStatus("disconnected");
        return;
      }
    }
    sessionRef.current.startRecording();
  }, []);

  const handleStop = useCallback(() => {
    sessionRef.current?.stopRecording();
  }, []);

  const handleEndSession = useCallback(() => {
    sessionRef.current?.endSession();
    // onClose fires async and resets sessionRef + status
  }, []);

  const sessionActive = status !== "disconnected";

  return (
    <main style={{ maxWidth: 600, margin: "80px auto", fontFamily: "sans-serif", padding: "0 24px" }}>
      <h1 style={{ marginBottom: 8 }}>Aloud</h1>
      <p style={{ color: "#666", marginBottom: 40 }}>Your voice journaling companion.</p>

      <VoiceButton status={status} onSpeak={handleSpeak} onStop={handleStop} />

      {sessionActive && (
        <p
          style={{
            textAlign: "center",
            color: status === "error" ? "#e53e3e" : "#718096",
            fontSize: 14,
            marginTop: -8,
            marginBottom: 8,
            minHeight: 20,
          }}
        >
          {status === "error" ? (errorMsg ?? "Something went wrong") : STATUS_LABEL[status]}
        </p>
      )}

      {sessionActive && (
        <p style={{ textAlign: "center", marginBottom: 32 }}>
          <button
            onClick={handleEndSession}
            style={{
              background: "none",
              border: "none",
              color: "#a0aec0",
              fontSize: 13,
              cursor: "pointer",
              textDecoration: "underline",
              padding: 0,
            }}
          >
            End Session
          </button>
        </p>
      )}

      <Transcript messages={messages} />
    </main>
  );
}
