"use client";

import { useState, useCallback, useRef } from "react";
import VoiceButton from "@/components/VoiceButton";
import Transcript from "@/components/Transcript";
import { Session, type SessionMessage } from "@/lib/session";

export default function Home() {
  const [active, setActive] = useState(false);
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const sessionRef = useRef<Session | null>(null);

  const handleToggle = useCallback(async () => {
    if (active) {
      sessionRef.current?.stop();
      sessionRef.current = null;
      setActive(false);
      return;
    }

    const session = new Session({
      wsUrl: process.env.NEXT_PUBLIC_BACKEND_WS_URL ?? "ws://localhost:8000",
      onMessage: (msg) => setMessages((prev) => [...prev, msg]),
      onClose: () => setActive(false),
    });

    await session.start();
    sessionRef.current = session;
    setActive(true);
  }, [active]);

  return (
    <main style={{ maxWidth: 600, margin: "80px auto", fontFamily: "sans-serif", padding: "0 24px" }}>
      <h1 style={{ marginBottom: 8 }}>Aloud</h1>
      <p style={{ color: "#666", marginBottom: 40 }}>Your voice journaling companion.</p>
      <VoiceButton active={active} onToggle={handleToggle} />
      <Transcript messages={messages} />
    </main>
  );
}
