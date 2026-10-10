"use client";

import { useRef, useState } from "react";

import { MarkdownView } from "@/components/MarkdownView";
import {
  MAX_TOTAL_CHARS,
  attachTotal,
  formatLabel,
  nextSort,
  sortDocs,
  type SortColumn,
  type SortDir,
  type WorkspaceDoc,
  type WorkspaceState,
} from "@/lib/workspace";

function formatChars(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  // short form (10/9/26) so the column tracks stay narrow
  return d.toLocaleDateString(undefined, {
    month: "numeric",
    day: "numeric",
    year: "2-digit",
  });
}

const SORT_LABELS: Record<SortColumn, string> = {
  name: "Name",
  type: "Type",
  source: "Source",
  created: "Created",
  updated: "Updated",
  size: "Size",
};

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
    <li className={`ws-row${previewed ? " previewed" : ""}`}>
      <button
        type="button"
        className="ws-cell ws-cell-name"
        title={doc.title}
        onClick={onPreview}
      >
        {doc.title}
      </button>
      <span className="ws-cell ws-cell-type">{formatLabel(doc.format)}</span>
      <span className="ws-cell ws-cell-source">
        {doc.source === "agent" ? "agent" : "upload"}
      </span>
      <span className="ws-cell ws-cell-date">{formatDate(doc.created_at)}</span>
      <span className="ws-cell ws-cell-date">
        {formatDate(doc.updated_at ?? doc.created_at)}
      </span>
      <span className="ws-cell ws-cell-size">{formatChars(doc.char_count)}</span>
      <span className="ws-cell ws-actions">
        {idle && (
          <button
            type="button"
            className={`ws-attach${attached ? " on" : ""}`}
            aria-pressed={attached}
            title={
              attached
                ? "Detach from next session"
                : "Attach to next session (kept in the agent's context)"
            }
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

/**
 * The document workspace (FR-54): ONE flat, column-structured list of the
 * user's documents (both sources), sortable by any column via the Sort
 * dropdown — each pick sorts by that column, picking it again reverses.
 * Per-row preview (row click), download, attach toggle (idle only), and
 * delete-behind-confirm; one preview pane, one document at a time.
 * Available while idle AND during a session; only upload and the attach
 * toggle are idle-gated.
 */
export function Workspace({
  state,
  idle,
  attachedIds,
  loading,
  error,
  onClose,
  onPreview,
  onClosePreview,
  onDownload,
  onDelete,
  onToggleAttach,
  onUpload,
}: {
  onClose: () => void;
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
  const [sort, setSort] = useState<{ col: SortColumn; dir: SortDir }>({
    col: "updated",
    dir: "desc", // newest activity first — matches the agent's own ordering
  });
  const [sortOpen, setSortOpen] = useState(false);

  const docs = sortDocs(state.docs, sort.col, sort.dir);
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

  const pickSort = (col: SortColumn) => {
    setSort((s) => nextSort(s, col));
    setSortOpen(false);
  };

  return (
    <aside className="workspace" aria-label="Documents">
      <div className="ws-bar">
        <h3 className="ws-heading">Documents</h3>
        <div className="ws-bar-right">
        <div className="ws-sort">
          <button
            type="button"
            className="ws-sort-btn"
            aria-haspopup="menu"
            aria-expanded={sortOpen}
            onClick={() => setSortOpen((o) => !o)}
          >
            sort: {SORT_LABELS[sort.col].toLowerCase()}{" "}
            {sort.dir === "asc" ? "↑" : "↓"}
          </button>
          {sortOpen && (
            <ul className="ws-sort-menu" role="menu">
              {(Object.keys(SORT_LABELS) as SortColumn[]).map((col) => (
                <li key={col}>
                  <button
                    type="button"
                    role="menuitem"
                    className={`ws-sort-item${sort.col === col ? " active" : ""}`}
                    onClick={() => pickSort(col)}
                  >
                    {SORT_LABELS[col]}
                    {sort.col === col ? (sort.dir === "asc" ? " ↑" : " ↓") : ""}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <button
          type="button"
          className="ws-close"
          aria-label="Close documents"
          onClick={onClose}
        >
          ×
        </button>
        </div>
      </div>

      {docs.length === 0 ? (
        <p className="ws-empty">{loading ? "loading…" : "no documents yet"}</p>
      ) : (
        <>
          <div className="ws-head ws-grid" aria-hidden>
            <span>Name</span>
            <span>Type</span>
            <span>Source</span>
            <span>Created</span>
            <span>Updated</span>
            <span>Size</span>
            <span />
          </div>
          <ul className="ws-table">
            {docs.map((d) => (
              <DocRow
                key={d.id}
                doc={d}
                idle={idle}
                attached={attachedIds.includes(d.id)}
                previewed={state.previewId === d.id}
                onPreview={() => onPreview(d)}
                onDownload={() => onDownload(d)}
                onDelete={() => onDelete(d)}
                onToggleAttach={() => onToggleAttach(d)}
              />
            ))}
          </ul>
        </>
      )}

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
