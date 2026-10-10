/**
 * Workspace state (FR-54/55) — a pure reducer over announce payloads and
 * server truth, kept free of React so the mandated client tests run as
 * plain functions: the upsert/tombstone/refetch rules are the branchiest
 * new client logic and each rule's failure is invisible in manual use.
 */

// Mirrors the backend's FR-21 budget (app/documents.py MAX_TOTAL_CHARS) —
// the client-side attach refusal is UX; the server bounds the fetch itself.
export const MAX_TOTAL_CHARS = 400_000;

export type WorkspaceDoc = {
  id: number;
  title: string;
  kind: string | null;
  format: string; // markdown | text | pdf
  source: "uploaded" | "agent";
  char_count: number;
  created_at?: string | null;
  updated_at?: string | null;
};

/** The announce payload's document (FR-53.9): metadata always, content
 * only under the server's cap. */
export type AnnouncedDoc = WorkspaceDoc & {
  content?: string | null;
  content_omitted?: boolean;
};

export type WorkspaceState = {
  docs: WorkspaceDoc[]; // activity order, newest first
  deletedIds: number[]; // session tombstones (FR-55)
  previewId: number | null;
  previewContent: string | null;
};

export const emptyWorkspace: WorkspaceState = {
  docs: [],
  deletedIds: [],
  previewId: null,
  previewContent: null,
};

/** Reload / fresh visit: the lists are server-backed truth (FR-55) —
 * replaces the docs wholesale and clears tombstones (the server already
 * reflects real deletes). An open preview survives only if its document
 * still exists. */
export function rehydrate(
  state: WorkspaceState,
  docs: WorkspaceDoc[],
): WorkspaceState {
  const previewAlive =
    state.previewId !== null && docs.some((d) => d.id === state.previewId);
  return {
    docs,
    deletedIds: [],
    previewId: previewAlive ? state.previewId : null,
    previewContent: previewAlive ? state.previewContent : null,
  };
}

/** FR-55's upsert: update if the id is on screen, insert if not — routed by
 * `source`, since an edited upload must land under Uploaded (the lists
 * derive from one activity-ordered array, so insert = prepend). Tombstoned
 * ids are ignored: FR-46 lets a write outlive its turn, and a late
 * document.updated must not resurrect a card the user deleted. Returns the
 * new state plus whether the previewed document needs a refetch (the
 * announce omitted oversized content — refetch ONLY then, and only while
 * that document is previewed). */
export function upsertFromAnnounce(
  state: WorkspaceState,
  doc: AnnouncedDoc,
): { state: WorkspaceState; refetch: boolean } {
  if (state.deletedIds.includes(doc.id)) {
    return { state, refetch: false };
  }
  const { content, content_omitted, ...meta } = doc;
  const docs = [meta, ...state.docs.filter((d) => d.id !== doc.id)];
  let previewContent = state.previewContent;
  let refetch = false;
  if (state.previewId === doc.id) {
    if (content_omitted) {
      refetch = true; // the client has no other way to refresh the pane
    } else if (typeof content === "string") {
      previewContent = content; // re-render in place — no refetch needed
    }
  }
  return { state: { ...state, docs, previewContent }, refetch };
}

/** A client-initiated hard delete (FR-52/55): the card goes, the id is
 * tombstoned for the session, and deleting the previewed document closes
 * the pane. */
export function markDeleted(
  state: WorkspaceState,
  id: number,
): WorkspaceState {
  return {
    docs: state.docs.filter((d) => d.id !== id),
    deletedIds: [...state.deletedIds, id],
    previewId: state.previewId === id ? null : state.previewId,
    previewContent: state.previewId === id ? null : state.previewContent,
  };
}

export function setPreview(
  state: WorkspaceState,
  id: number | null,
  content: string | null = null,
): WorkspaceState {
  return { ...state, previewId: id, previewContent: content };
}

/** The visible attach budget (FR-51): sum of attached char counts against
 * MAX_TOTAL_CHARS. The toggle refuses additions that would cross it. */
export function attachTotal(docs: WorkspaceDoc[], attachedIds: number[]): number {
  return docs
    .filter((d) => attachedIds.includes(d.id))
    .reduce((sum, d) => sum + d.char_count, 0);
}

/** FR-52's download filename: derives from an untrusted 🔒 title — path
 * separators and control characters stripped, length-capped, empty →
 * document-{id}; the extension is .md for markdown and .txt for text AND
 * pdf (stored content is extracted text), and a title already ending with
 * it gets nothing appended (plan.md stays plan.md, never plan.md.md). */
export function downloadFilename(
  title: string,
  format: string,
  id: number,
): string {
  const ext = format === "markdown" ? ".md" : ".txt";
  const cleaned = title
    // eslint-disable-next-line no-control-regex
    .replace(/[/\\:*?"<>|\u0000-\u001f]/g, "")
    .trim()
    .slice(0, 120);
  if (!cleaned) return `document-${id}${ext}`;
  if (cleaned.toLowerCase().endsWith(ext)) return cleaned;
  return cleaned + ext;
}

/* ---- list sorting (FR-54 UI) ---- */

export type SortColumn =
  | "name"
  | "type"
  | "source"
  | "created"
  | "updated"
  | "size";
export type SortDir = "asc" | "desc";

/** Display format: plain file-type for every source (agent docs are
 * markdown, so "md"). */
export function formatLabel(format: string): string {
  if (format === "markdown") return "md";
  if (format === "text") return "txt";
  return format;
}

const NATURAL_DESC: SortColumn[] = ["created", "updated", "size"];

/** Each click sorts — never idempotent: a new column takes its natural
 * first direction (alphabetical for text, newest/largest first for dates
 * and size); clicking the already-active column reverses it. */
export function nextSort(
  current: { col: SortColumn; dir: SortDir },
  clicked: SortColumn,
): { col: SortColumn; dir: SortDir } {
  if (current.col === clicked) {
    return { col: clicked, dir: current.dir === "asc" ? "desc" : "asc" };
  }
  return { col: clicked, dir: NATURAL_DESC.includes(clicked) ? "desc" : "asc" };
}

function activityOf(d: WorkspaceDoc): string {
  return d.updated_at ?? d.created_at ?? "";
}

export function sortDocs(
  docs: WorkspaceDoc[],
  col: SortColumn,
  dir: SortDir,
): WorkspaceDoc[] {
  const sorted = [...docs].sort((a, b) => {
    let cmp: number;
    switch (col) {
      case "name":
        cmp = a.title.localeCompare(b.title, undefined, { sensitivity: "base" });
        break;
      case "type":
        cmp = formatLabel(a.format).localeCompare(formatLabel(b.format));
        break;
      case "source":
        cmp = a.source.localeCompare(b.source);
        break;
      case "created":
        cmp = (a.created_at ?? "").localeCompare(b.created_at ?? "");
        break;
      case "updated":
        cmp = activityOf(a).localeCompare(activityOf(b));
        break;
      case "size":
        cmp = a.char_count - b.char_count;
        break;
    }
    // stable tie-break so equal keys keep a deterministic order
    if (cmp === 0) cmp = a.id - b.id;
    return dir === "asc" ? cmp : -cmp;
  });
  return sorted;
}
