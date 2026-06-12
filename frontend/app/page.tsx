"use client";

import { ArtifactsPanel } from "@/components/ArtifactsPanel";
import { SessionButton } from "@/components/SessionButton";
import { WaveformBar } from "@/components/WaveformBar";
import { useAloudSession } from "@/lib/useAloudSession";

const STATUS: Record<string, string> = {
  idle: "tap to start thinking out loud",
  connecting: "warming up…",
  ending: "wrapping up…",
  listening: "listening",
  thinking: "thinking",
  speaking: "speaking",
};

export default function Home() {
  const { state, mode, error, localTrack, botTrack, artifacts, talk, end } =
    useAloudSession();

  const status = state === "active" ? STATUS[mode] : STATUS[state];

  return (
    <main className="stage">
      <header className="masthead">
        <h1 className="wordmark">Aloud</h1>
        <p className="tagline">a place to think out loud</p>
      </header>

      <ArtifactsPanel artifacts={artifacts} />

      <div className="core">
        <SessionButton state={state} onTalk={talk} onEnd={end} />
        <WaveformBar
          active={state === "active"}
          mode={mode}
          localTrack={localTrack}
          botTrack={botTrack}
        />
        <p className={`status ${state === "active" ? mode : state}`}>{status}</p>
        {error && <p className="error">{error}</p>}
      </div>

      <footer className="foot" aria-hidden>
        <span>aloud</span>
        <span>·</span>
        <span>session console</span>
      </footer>
    </main>
  );
}
