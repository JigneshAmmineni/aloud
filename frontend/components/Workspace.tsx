"use client";

import { useRef, useState } from "react";

import { EditorPane } from "@/components/EditorPane";
import {
  MAX_TOTAL_CHARS,
  nextSort,
  sortDocs,
  type SortColumn,
  type SortDir,
  type WorkspaceDoc,
  type WorkspaceState,
} from "@/lib/workspace";
import type { EditorTab } from "@/lib/useEditor";

function formatChars(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;
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
  open,
  onOpen,
  onDownload,
  onDelete,
  onToggleAttach,
}: {
  doc: WorkspaceDoc;
  idle: boolean;
  attached: boolean;
  open: boolean;
  onOpen: () => void;
  onDownload: () => void;
  onDelete: () => void;
  onToggleAttach: () => void;
}) {
  // FR-54: delete is behind an explicit confirmation, visually distinct
  // from the attach toggle.
  const [confirming, setConfirming] = useState(false);
  return (
    <li className={`ws-row${open ? " previewed" : ""}`}>
      <button
        type="button"
        className="ws-cell ws-cell-name"
        title={doc.title}
        onClick={onOpen}
      >
        {doc.title}
      </button>
      {/* FR-57.1: char_count stays on the row — the budget's unit; a
          refusal must let the user see WHICH document to drop */}
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
 * FR-57: the workspace — names + size list in the left pane (with the
 * console docked beneath it, FR-57.4), and the tabbed editor pane beside
 * it. The sort control is a field picker (FR-54's click-a-column
 * affordance retired with the columns).
 */
export function Workspace({
  state,
  idle,
  attachedIds,
  attachedChars,
  total,
  loading,
  error,
  budgetError,
  console: consoleSlot,
  editor,
  onClose,
  onLoadMore,
  onOpen,
  onDownload,
  onDelete,
  onToggleAttach,
  onUpload,
  onUploadsSettled,
}: {
  state: WorkspaceState;
  idle: boolean;
  attachedIds: number[];
  attachedChars: number;
  total: number;
  loading: boolean;
  error: string | null;
  budgetError: string | null;
  /** FR-57.4: the session console docks into the list pane's bottom */
  console: React.ReactNode;
  editor: {
    tabs: EditorTab[];
    activeId: number | null;
    onActivate: (id: number) => void;
    onClose: (id: number) => void;
    onEdit: (id: number, text: string) => void;
    onSetView: (id: number, view: "source" | "preview") => void;
    onApplyTheirs: (id: number) => void;
    onDismissConflict: (id: number) => void;
  };
  onClose: () => void;
  onLoadMore: () => void;
  onOpen: (d: WorkspaceDoc) => void;
  onDownload: (d: WorkspaceDoc) => void;
  onDelete: (d: WorkspaceDoc) => void;
  onToggleAttach: (d: WorkspaceDoc) => void;
  onUpload: (file: File) => Promise<void>;
  onUploadsSettled: () => Promise<void>;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [sort, setSort] = useState<{ col: SortColumn; dir: SortDir }>({
    col: "updated",
    dir: "desc",
  });
  const [sortOpen, setSortOpen] = useState(false);

  const docs = sortDocs(state.docs, sort.col, sort.dir);
  const openIds = new Set(editor.tabs.map((t) => t.id));

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
      await onUploadsSettled().catch(() => {});
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const pickSort = (col: SortColumn) => {
    setSort((s) => nextSort(s, col));
    setSortOpen(false);
  };

  return (
    <aside
      className={`workspace ws-v2${editor.tabs.length ? " has-tabs" : ""}`}
      aria-label="Documents"
    >
      <div className="ws-left">
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
                        {sort.col === col
                          ? sort.dir === "asc"
                            ? " ↑"
                            : " ↓"
                          : ""}
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

        <div className="ws-list-scroll">
          {docs.length === 0 ? (
            <p className="ws-empty">{loading ? "loading…" : "no documents yet"}</p>
          ) : (
            <ul className="ws-table">
              {docs.map((d) => (
                <DocRow
                  key={d.id}
                  doc={d}
                  idle={idle}
                  attached={attachedIds.includes(d.id)}
                  open={openIds.has(d.id)}
                  onOpen={() => onOpen(d)}
                  onDownload={() => onDownload(d)}
                  onDelete={() => onDelete(d)}
                  onToggleAttach={() => onToggleAttach(d)}
                />
              ))}
            </ul>
          )}
          {state.docs.length < total && (
            <button type="button" className="ws-more" onClick={onLoadMore}>
              show older ({total - state.docs.length} more)
            </button>
          )}
        </div>

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
            attached: {formatChars(attachedChars)} /{" "}
            {formatChars(MAX_TOTAL_CHARS)} chars
          </p>
        )}
        {budgetError && <p className="doc-error">{budgetError}</p>}
        {error && <p className="doc-error">{error}</p>}

        {/* FR-57.4: the console docks here — Talk/End, waveform, status,
            fully live while the user edits */}
        <div className="ws-console">{consoleSlot}</div>
      </div>

      <EditorPane
        tabs={editor.tabs}
        activeId={editor.activeId}
        onActivate={editor.onActivate}
        onClose={editor.onClose}
        onEdit={editor.onEdit}
        onSetView={editor.onSetView}
        onApplyTheirs={editor.onApplyTheirs}
        onDismissConflict={editor.onDismissConflict}
      />
    </aside>
  );
}
