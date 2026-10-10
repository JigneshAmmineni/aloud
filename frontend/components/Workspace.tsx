"use client";

import { useRef, useState } from "react";

import { MarkdownView } from "@/components/MarkdownView";
import {
  MAX_TOTAL_CHARS,
  attachTotal,
  type WorkspaceDoc,
  type WorkspaceState,
} from "@/lib/workspace";

const KIND_LABELS: Record<string, string> = {
  summary: "summary",
  action_items: "action items",
  cleaned_idea: "cleaned-up idea",
};

function formatChars(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
}

function badge(doc: WorkspaceDoc): string {
  if (doc.kind) return KIND_LABELS[doc.kind] ?? doc.kind;
  return doc.format;
}

function DocRow({
  doc,
  idle,
  attached,
  previewed,
  onPreview,
  onDownload,
  onDelete,
  onToggleAttach,
}: {
  doc: WorkspaceDoc;
  idle: boolean;
  attached: boolean;
  previewed: boolean;
  onPreview: () => void;
  onDownload: () => void;
  onDelete: () => void;
  onToggleAttach: () => void;
}) {
  // FR-54: delete is behind an explicit confirmation, visually distinct
  // from the attach toggle — removing-from-session and destroying-the-row
  // must never be mistaken for each other.
  const [confirming, setConfirming] = useState(false);
  return (
    <li className={`ws-item${previewed ? " previewed" : ""}`}>
      <button type="button" className="ws-item-main" onClick={onPreview}>
        <span className="ws-title">{doc.title}</span>
        <span className="ws-meta">
          {badge(doc)} · {formatChars(doc.char_count)} chars
        </span>
      </button>
      <span className="ws-actions">
        {idle && (
          <button
            type="button"
            className={`ws-attach${attached ? " on" : ""}`}
            aria-pressed={attached}
            title={attached ? "Detach from next session" : "Attach to next session"}
            onClick={onToggleAttach}
          >
            {attached ? "attached" : "attach"}
          </button>
        )}
        <button type="button" className="ws-dl" title="Download" onClick={onDownload}>
          ↓
        </button>
        {confirming ? (
          <span className="ws-confirm">
            <button type="button" className="ws-delete-yes" onClick={onDelete}>
              delete?
            </button>
            <button
              type="button"
              className="ws-delete-no"
              onClick={() => setConfirming(false)}
            >
              keep
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="ws-delete"
            title="Delete permanently"
            aria-label={`Delete ${doc.title}`}
            onClick={() => setConfirming(true)}
          >
            ×
          </button>
        )}
      </span>
    </li>
  );
}

function DocList({
  heading,
  docs,
  empty,
  children,
  ...rowProps
}: {
  heading: string;
  docs: WorkspaceDoc[];
  empty: string;
  children?: React.ReactNode;
  idle: boolean;
  attachedIds: number[];
  previewId: number | null;
  onPreview: (d: WorkspaceDoc) => void;
  onDownload: (d: WorkspaceDoc) => void;
  onDelete: (d: WorkspaceDoc) => void;
  onToggleAttach: (d: WorkspaceDoc) => void;
}) {
  return (
    <section className="ws-list" aria-label={heading}>
      <h3 className="ws-heading">{heading}</h3>
      {docs.length === 0 ? (
        <p className="ws-empty">{empty}</p>
      ) : (
        <ul>
          {docs.map((d) => (
            <DocRow
              key={d.id}
              doc={d}
              idle={rowProps.idle}
              attached={rowProps.attachedIds.includes(d.id)}
              previewed={rowProps.previewId === d.id}
              onPreview={() => rowProps.onPreview(d)}
              onDownload={() => rowProps.onDownload(d)}
              onDelete={() => rowProps.onDelete(d)}
              onToggleAttach={() => rowProps.onToggleAttach(d)}
            />
          ))}
        </ul>
      )}
      {children}
    </section>
  );
}

/**
 * The document workspace (FR-54): two side-by-side scrollable lists —
 * Uploaded and Created — with per-item preview, download, attach toggle
 * (idle only), and delete-behind-confirm; one preview pane, one document
 * at a time. Available while idle AND during a session; only upload and
 * the attach toggle are idle-gated.
 */
export function Workspace({
  state,
  idle,
  attachedIds,
  loading,
  error,
  onPreview,
  onClosePreview,
  onDownload,
  onDelete,
  onToggleAttach,
  onUpload,
}: {
  state: WorkspaceState;
  idle: boolean;
  attachedIds: number[];
  loading: boolean;
  error: string | null;
  onPreview: (d: WorkspaceDoc) => void;
  onClosePreview: () => void;
  onDownload: (d: WorkspaceDoc) => void;
  onDelete: (d: WorkspaceDoc) => void;
  onToggleAttach: (d: WorkspaceDoc) => void;
  onUpload: (file: File) => Promise<void>;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const uploaded = state.docs.filter((d) => d.source === "uploaded");
  const created = state.docs.filter((d) => d.source === "agent");
  const previewDoc =
    state.previewId === null
      ? null
      : (state.docs.find((d) => d.id === state.previewId) ?? null);
  const total = attachTotal(state.docs, attachedIds);

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploadError(null);
    setBusy(true);
    try {
      for (const file of Array.from(files)) {
        await onUpload(file);
      }
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : "couldn't read that file");
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const rowProps = {
    idle,
    attachedIds,
    previewId: state.previewId,
    onPreview,
    onDownload,
    onDelete,
    onToggleAttach,
  };

  return (
    <aside className="workspace" aria-label="Documents">
      <div className="ws-lists">
        <DocList
          heading="Uploaded"
          docs={uploaded}
          empty={loading ? "loading…" : "no uploads yet"}
          {...rowProps}
        >
          {idle && (
            <>
              <button
                type="button"
                className="doc-attach"
                disabled={busy}
                onClick={() => inputRef.current?.click()}
              >
                {busy ? "reading…" : "+ upload a document"}
              </button>
              <input
                ref={inputRef}
                type="file"
                accept=".txt,.md,.markdown,.pdf,text/plain,text/markdown,application/pdf"
                multiple
                hidden
                onChange={(e) => handleFiles(e.target.files)}
              />
              {uploadError && <p className="doc-error">{uploadError}</p>}
            </>
          )}
        </DocList>
        <DocList
          heading="Created"
          docs={created}
          empty={loading ? "loading…" : "nothing written up yet"}
          {...rowProps}
        />
      </div>
      {idle && attachedIds.length > 0 && (
        <p className="ws-budget">
          attached: {formatChars(total)} / {formatChars(MAX_TOTAL_CHARS)} chars
        </p>
      )}
      {error && <p className="doc-error">{error}</p>}

      {previewDoc && (
        <section className="ws-preview" aria-label={`Preview: ${previewDoc.title}`}>
          <header className="ws-preview-head">
            <span className="ws-title">{previewDoc.title}</span>
            <button
              type="button"
              className="ws-preview-close"
              aria-label="Close preview"
              onClick={onClosePreview}
            >
              ×
            </button>
          </header>
          {state.previewContent === null ? (
            <p className="ws-empty">loading…</p>
          ) : previewDoc.format === "markdown" ? (
            <MarkdownView content={state.previewContent} />
          ) : (
            <pre className="ws-pre">{state.previewContent}</pre>
          )}
        </section>
      )}
    </aside>
  );
}
