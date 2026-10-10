"use client";

import { useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { signOut } from "firebase/auth";

import { SessionButton } from "@/components/SessionButton";
import { WaveformBar } from "@/components/WaveformBar";
import { Workspace } from "@/components/Workspace";
import { useAuth } from "@/lib/auth";
import { auth } from "@/lib/firebase";
import { useAloudSession } from "@/lib/useAloudSession";
import { useWorkspace } from "@/lib/useWorkspace";

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
  const workspace = useWorkspace(!loading && !!user);
  const {
    state,
    mode,
    error,
    localTrack,
    botTrack,
    talk,
    end,
  } = useAloudSession({ onDocumentAnnounce: workspace.handleAnnounce });

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
        <p className="tagline">a place to work out loud</p>
      </header>

      <div className="core">
        <SessionButton
          state={state}
          onTalk={() => talk(workspace.attachedIds)}
          onEnd={end}
        />
        <WaveformBar
          active={state === "active"}
          mode={mode}
          localTrack={localTrack}
          botTrack={botTrack}
        />
        <p className={`status ${state === "active" ? mode : state}`}>{status}</p>
        {error && <p className="error">{error}</p>}
      </div>

      {/* FR-54: the workspace and preview are available while idle AND
          during a session; only upload and the attach toggle are idle-gated. */}
      <Workspace
        state={workspace.state}
        idle={state === "idle"}
        attachedIds={workspace.attachedIds}
        loading={workspace.loading}
        error={workspace.error}
        onPreview={workspace.preview}
        onClosePreview={workspace.closePreview}
        onDownload={workspace.download}
        onDelete={workspace.deleteDoc}
        onToggleAttach={workspace.toggleAttach}
        onUpload={workspace.upload}
      />

      <footer className="foot" aria-hidden>
        <span>aloud</span>
        <span>·</span>
        <span>session console</span>
      </footer>
    </main>
  );
}
