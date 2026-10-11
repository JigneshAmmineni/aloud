"use client";

import { markdown } from "@codemirror/lang-markdown";
import CodeMirror from "@uiw/react-codemirror";

import { MarkdownView } from "@/components/MarkdownView";
import type { EditorTab } from "@/lib/useEditor";
import { downloadFilename } from "@/lib/workspace";

const SAVE_LABEL: Record<EditorTab["saveState"], string> = {
  saved: "saved",
  saving: "saving…",
  unsaved: "unsaved",
  error: "couldn't save",
};

function downloadBuffer(tab: EditorTab) {
  const blob = new Blob([tab.buffer], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  // the one path where the buffer is the only copy — same sanitized,
  // never-double-extended filename as the list's download
  a.download = downloadFilename(tab.title, tab.format, tab.id);
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    a.remove();
    URL.revokeObjectURL(url);
  }, 1000);
}

/**
 * FR-57: the docked editor pane — tabs (one per document), CodeMirror
 * for md/text, the sanitized preview as markdown's toggle (never a
 * second render path), pdf read-only, explicit load/error/save states,
 * and FR-58's conflict banner with "apply theirs".
 */
export function EditorPane({
  tabs,
  activeId,
  onActivate,
  onClose,
  onEdit,
  onSetView,
  onApplyTheirs,
  onDismissConflict,
  onRename,
  onRetryStalled,
}: {
  tabs: EditorTab[];
  activeId: number | null;
  onActivate: (id: number) => void;
  onClose: (id: number) => void;
  onEdit: (id: number, text: string) => void;
  onSetView: (id: number, view: "source" | "preview") => void;
  onApplyTheirs: (id: number) => void;
  onDismissConflict: (id: number) => void;
  onRename: (id: number, title: string) => void;
  onRetryStalled: (id: number) => void;
}) {
  const active = tabs.find((t) => t.id === activeId) ?? null;
  if (tabs.length === 0) {
    return (
      <section className="ed-pane" aria-label="Editor">
        <p className="ws-empty ed-empty">
          open a document from the list to read or edit it
        </p>
      </section>
    );
  }

  const editable =
    !!active &&
    active.format !== "pdf" &&
    !active.loading &&
    !active.loadFailed &&
    !active.terminal;

  return (
    <section className="ed-pane" aria-label="Editor">
      <div className="ed-tabs" role="tablist">
        {tabs.map((t) => (
          <span
            key={t.id}
            className={`ed-tab${t.id === activeId ? " active" : ""}`}
          >
            <button
              type="button"
              role="tab"
              aria-selected={t.id === activeId}
              className="ed-tab-name"
              title={t.title}
              onClick={() => onActivate(t.id)}
            >
              {t.dirty ? "• " : ""}
              {t.title}
            </button>
            <button
              type="button"
              className="ed-tab-close"
              aria-label={`Close ${t.title}`}
              onClick={() => onClose(t.id)}
            >
              ×
            </button>
          </span>
        ))}
      </div>

      {active && (
        <>
          <div className="ed-bar">
            <input
              key={active.id}
              className="ed-title"
              defaultValue={active.title}
              aria-label="Document title"
              title="Click to rename"
              disabled={!editable}
              onBlur={(e) => {
                const v = e.target.value.trim();
                if (v && v !== active.title) onRename(active.id, v);
                else e.target.value = active.title;
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              }}
            />
            {active.format === "markdown" && editable && (
              <span className="ed-toggle" role="group" aria-label="View">
                <button
                  type="button"
                  className={active.view === "preview" ? "on" : ""}
                  title="Preview"
                  aria-label="Preview"
                  onClick={() => onSetView(active.id, "preview")}
                >
                  👁
                </button>
                <button
                  type="button"
                  className={active.view === "source" ? "on" : ""}
                  title="Source"
                  aria-label="Source"
                  onClick={() => onSetView(active.id, "source")}
                >
                  {"</>"}
                </button>
              </span>
            )}
            {active.format === "pdf" && (
              <span className="ed-meta">read-only (extracted text)</span>
            )}
            <span
              className={`ed-save ed-save-${active.saveState}`}
              aria-live="polite"
            >
              {active.loading || active.loadFailed
                ? ""
                : SAVE_LABEL[active.saveState]}
            </span>
          </div>

          {active.stalled && (
            <div className="ed-banner ed-terminal" role="alert">
              someone else is editing this document — your text is kept.
              <button type="button" onClick={() => onRetryStalled(active.id)}>
                try again
              </button>
              <button type="button" onClick={() => downloadBuffer(active)}>
                download my copy
              </button>
            </div>
          )}
          {active.conflict && (
            <div className="ed-banner" role="alert">
              the agent edited this while you were typing — your version is
              kept.
              <button type="button" onClick={() => onApplyTheirs(active.id)}>
                apply theirs
              </button>
              <button
                type="button"
                onClick={() => onDismissConflict(active.id)}
              >
                keep mine
              </button>
            </div>
          )}
          {active.terminal && (
            <div className="ed-banner ed-terminal" role="alert">
              {active.terminal}
              <button type="button" onClick={() => downloadBuffer(active)}>
                download my copy
              </button>
            </div>
          )}

          <div className="ed-body">
            {active.loading ? (
              <p className="ws-empty">loading…</p>
            ) : active.loadFailed ? (
              // FR-57.3's guard: never a blank EDITABLE buffer over a
              // document that failed to load
              <p className="doc-error">
                couldn't load this document — close the tab and try again
              </p>
            ) : active.format === "markdown" && active.view === "preview" ? (
              <div
                className="ed-preview"
                onDoubleClick={() => onSetView(active.id, "source")}
                title="Double-click to edit"
              >
                <MarkdownView content={active.buffer} />
              </div>
            ) : active.format === "pdf" ? (
              <pre className="ws-pre">{active.buffer}</pre>
            ) : (
              <CodeMirror
                value={active.buffer}
                onChange={(value) => onEdit(active.id, value)}
                extensions={
                  active.format === "markdown" ? [markdown()] : []
                }
                theme="dark"
                basicSetup={{ lineNumbers: true, foldGutter: false }}
                editable={editable}
              />
            )}
          </div>
        </>
      )}
    </section>
  );
}
