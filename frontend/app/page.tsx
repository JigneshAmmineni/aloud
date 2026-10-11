"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { signOut } from "firebase/auth";

import { SessionButton } from "@/components/SessionButton";
import { WaveformBar } from "@/components/WaveformBar";
import { Workspace } from "@/components/Workspace";
import { useAuth } from "@/lib/auth";
import { auth } from "@/lib/firebase";
import { useAloudSession } from "@/lib/useAloudSession";
import { useEditor } from "@/lib/useEditor";
import { useWorkspace } from "@/lib/useWorkspace";
import { guardTalk, type AnnouncedDoc, type WorkspaceDoc } from "@/lib/workspace";

const STATUS: Record<string, string> = {
  idle: "",
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
  const editor = useEditor({ onSaved: workspace.noteSaved });
  const [docsOpen, setDocsOpen] = useState(false);
  const [flushError, setFlushError] = useState<string | null>(null);

  const workspaceAnnounce = workspace.handleAnnounce;
  const editorAnnounce = editor.handleAnnounce;
  // FR-55/58.2: announces reach the list AND any open editor tab
  const onDocumentAnnounce = useCallback(
    (type: string, doc: AnnouncedDoc) => {
      workspaceAnnounce(type, doc);
      editorAnnounce(type, doc);
    },
    [workspaceAnnounce, editorAnnounce],
  );

  const { state, mode, error, localTrack, botTrack, talk, end } =
    useAloudSession({ onDocumentAnnounce });

  // FR-30: unauthenticated visits land on /login.
  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  // FR-58.1: while unsaved changes exist, leaving the page warns
  const anyDirty = editor.tabs.some((t) => t.dirty);
  useEffect(() => {
    if (!anyDirty) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = ""; // WebKit needs it — NFR-3 names mobile first
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [anyDirty]);

  // FR-58.1: the pre-/start flush — Talk refuses on a failed flush
  // rather than opening the session on superseded text
  const flushAll = editor.flushAll;
  const attachedIds = workspace.attachedIds;
  const guardedTalk = useCallback(async () => {
    setFlushError(null);
    await guardTalk(flushAll, talk, attachedIds, setFlushError);
  }, [flushAll, talk, attachedIds]);

  // in-app navigation is a flush point too (client-side transitions
  // never fire beforeunload)
  const flushThen = useCallback(
    (navigate: () => void) => async () => {
      await flushAll();
      navigate();
    },
    [flushAll],
  );

  const deleteDoc = workspace.deleteDoc;
  const closeDeleted = editor.closeDeleted;
  const handleDelete = useCallback(
    async (doc: WorkspaceDoc) => {
      // only a SUCCESSFUL delete closes the tab — a failed one must not
      // drop the only copy of an unsaved buffer (round-2 blocker 3)
      if (await deleteDoc(doc)) {
        closeDeleted(doc.id); // a deleted document closes its tab (FR-57.3)
      }
    },
    [deleteDoc, closeDeleted],
  );

  const status = state === "active" ? STATUS[mode] : STATUS[state];

  if (loading || !user) return null;

  const consoleUI = () => (
    <>
      <SessionButton state={state} onTalk={guardedTalk} onEnd={end} />
      <WaveformBar
        active={state === "active"}
        mode={mode}
        localTrack={localTrack}
        botTrack={botTrack}
      />
      <p className={`status ${state === "active" ? mode : state}`}>{status}</p>
      {error && <p className="error">{error}</p>}
      {flushError && <p className="error">{flushError}</p>}
    </>
  );

  return (
    <main className="stage">
      <nav className="topnav" aria-label="account">
        <button
          type="button"
          className="login-link"
          aria-expanded={docsOpen}
          onClick={() => setDocsOpen((o) => !o)}
        >
          Documents
        </button>
        {isAdmin && (
          <button
            type="button"
            className="login-link"
            onClick={flushThen(() => router.push("/admin"))}
          >
            Admin
          </button>
        )}
        <button
          type="button"
          className="login-link"
          onClick={flushThen(() => void signOut(auth))}
        >
          Sign out
        </button>
      </nav>
      <header className="masthead">
        <h1 className="wordmark">Aloud</h1>
        <p className="tagline">a place to work out loud</p>
      </header>

      {/* FR-57.4: with the workspace open the console docks into it;
          closed, the full-size console owns the stage */}
      {!docsOpen && <div className="core">{consoleUI()}</div>}

      {docsOpen && (
        <Workspace
          state={workspace.state}
          idle={state === "idle"}
          attachedIds={workspace.attachedIds}
          attachedChars={workspace.attachedChars}
          total={workspace.total}
          loading={workspace.loading}
          error={workspace.error}
          budgetError={workspace.budgetError}
          console={consoleUI()}
          editor={{
            tabs: editor.tabs,
            activeId: editor.activeId,
            onActivate: editor.activate,
            onClose: editor.close,
            onEdit: editor.edit,
            onSetView: editor.setView,
            onApplyTheirs: editor.applyTheirs,
            onDismissConflict: editor.dismissConflict,
            onRename: editor.rename,
            onRetryStalled: editor.retryStalled,
            onDiscard: editor.discard,
          }}
          onClose={() => {
            // FR-58.1: closing the workspace is a flush point
            void flushAll();
            setDocsOpen(false);
          }}
          onLoadMore={workspace.loadMore}
          onOpen={editor.open}
          onDownload={workspace.download}
          onDelete={handleDelete}
          onToggleAttach={workspace.toggleAttach}
          onUpload={workspace.upload}
          onUploadsSettled={() => workspace.refresh()}
        />
      )}

      <footer className="foot" aria-hidden>
        <span>aloud</span>
        <span>·</span>
        <span>session console</span>
      </footer>
    </main>
  );
}
