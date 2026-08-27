"use client";

import { useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { signOut } from "firebase/auth";

import { ArtifactsPanel } from "@/components/ArtifactsPanel";
import { DocumentUpload } from "@/components/DocumentUpload";
import { SessionButton } from "@/components/SessionButton";
import { WaveformBar } from "@/components/WaveformBar";
import { useAuth } from "@/lib/auth";
import { auth } from "@/lib/firebase";
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
  const router = useRouter();
  const { user, loading, isAdmin } = useAuth();
  const {
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
  } = useAloudSession();

  // FR-30: unauthenticated visits land on /login.
  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  const status = state === "active" ? STATUS[mode] : STATUS[state];

  if (loading || !user) return null;

  return (
    <main className="stage">
      <nav className="topnav" aria-label="account">
        {isAdmin && (
          <Link className="login-link" href="/admin">
            Admin
          </Link>
        )}
        <button
          type="button"
          className="login-link"
          onClick={() => signOut(auth)}
        >
          Sign out
        </button>
      </nav>
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
        {state === "idle" && (
          <DocumentUpload
            documents={documents}
            onUpload={uploadDocument}
            onRemove={removeDocument}
          />
        )}
      </div>

      <footer className="foot" aria-hidden>
        <span>aloud</span>
        <span>·</span>
        <span>session console</span>
      </footer>
    </main>
  );
}
