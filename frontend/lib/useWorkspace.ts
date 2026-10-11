"use client";

/**
 * The workspace hook (FR-54/55): server-backed document state, announce
 * upserts, attach selection with the visible budget refusal, upload,
 * delete, preview, and download. The decision logic lives in
 * lib/workspace.ts as pure functions; this hook owns the IO.
 *
 * Attach bookkeeping lives in ONE synchronous map (id -> char_count),
 * mutated only in event handlers and mirrored into state for rendering.
 * That is what makes a multi-file batch see its own earlier files
 * (round-2 blocker: a stale state snapshot let three 200k uploads attach
 * against a 400k budget), keeps the budget honest even for an attached
 * row the visible list no longer carries, and keeps every setState
 * updater pure (no setError/ref writes inside — StrictMode double-invokes
 * updaters).
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { authedFetch } from "@/lib/auth";
import {
  MAX_TOTAL_CHARS,
  downloadFilename,
  emptyWorkspace,
  markDeleted,
  rehydrate,
  updateDocMeta,
  upsertFromAnnounce,
  type AnnouncedDoc,
  type WorkspaceDoc,
  type WorkspaceState,
} from "@/lib/workspace";

async function fetchContent(id: number): Promise<string | null> {
  const res = await authedFetch(`/documents/${id}`);
  if (!res.ok) return null;
  const body = (await res.json()) as { content: string };
  return body.content;
}

export function useWorkspace(enabled: boolean) {
  const [state, setState] = useState<WorkspaceState>(emptyWorkspace);
  const [attachedIds, setAttachedIds] = useState<number[]>([]);
  const [attachedChars, setAttachedChars] = useState(0);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // the budget refusal has its OWN slot: refresh()'s success path clears
  // `error`, and round 3's blocker was exactly that wiping the refusal a
  // breath after tryAttach raised it
  const [budgetError, setBudgetError] = useState<string | null>(null);

  // The single source of attach truth: id -> char_count. Synchronous, so
  // batched uploads and budget math never race React's render cycle.
  const attachedRef = useRef<Map<number, number>>(new Map());
  // Mirrors for reads from async code (announce callbacks, uploads).
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  const syncAttached = useCallback(() => {
    const m = attachedRef.current;
    setAttachedIds([...m.keys()]);
    setAttachedChars([...m.values()].reduce((a, b) => a + b, 0));
  }, []);

  /** Attach under the budget; returns false (and names the refusal) when
   * it does not fit. The map makes this correct mid-batch. */
  const tryAttach = useCallback(
    (id: number, charCount: number, refusal: string): boolean => {
      const m = attachedRef.current;
      if (m.has(id)) return true;
      const used = [...m.values()].reduce((a, b) => a + b, 0);
      if (used + charCount > MAX_TOTAL_CHARS) {
        setBudgetError(refusal);
        return false;
      }
      m.set(id, charCount);
      setBudgetError(null);
      syncAttached();
      return true;
    },
    [syncAttached],
  );

  /** Fetch from offset 0 until at least `minRows` rows (or the corpus) are
   * covered — a refresh must not collapse pages "show older" already
   * fetched, or an attached row from a dropped page goes invisible while
   * still attaching (round-2 finding 4). */
  const refresh = useCallback(async (minRows = 0) => {
    try {
      const target = Math.max(minRows, stateRef.current.docs.length);
      let docs: WorkspaceDoc[] = [];
      let serverTotal = 0;
      do {
        const res = await authedFetch(`/documents?offset=${docs.length}`);
        if (!res.ok) throw new Error();
        const body = (await res.json()) as {
          documents: WorkspaceDoc[];
          total: number;
        };
        serverTotal = body.total;
        const known = new Set(docs.map((d) => d.id));
        const fresh = body.documents.filter((d) => !known.has(d.id));
        // no growth: never loop on a shifting window of rows already seen
        if (fresh.length === 0) break;
        docs = [...docs, ...fresh];
      } while (docs.length < target && docs.length < serverTotal);
      setState((s) => rehydrate(s, docs));
      setTotal(serverTotal);
      setError(null);
    } catch {
      setError("couldn't load your documents");
    } finally {
      setLoading(false);
    }
  }, []);

  // FR-55: on load (and reload) the workspace rehydrates from GET /documents
  useEffect(() => {
    if (enabled) void refresh();
  }, [enabled, refresh]);

  /** Wired into the session's onServerMessage: FR-55's upsert rules for
   * the LIST; the editor hook handles open tabs (content/merge/refetch —
   * §4.12 extends the pane rules to editor tabs). */
  const handleAnnounce = useCallback(
    (type: string, doc: AnnouncedDoc) => {
      if (type !== "document.created" && type !== "document.updated") return;
      // an edited document's new size must reach the attach accounting —
      // the announce ships char_count for exactly this
      const m = attachedRef.current;
      if (m.has(doc.id) && typeof doc.char_count === "number") {
        m.set(doc.id, doc.char_count);
        syncAttached();
      }
      setState((prev) => upsertFromAnnounce(prev, doc));
    },
    [syncAttached],
  );

  /** FR-56: the user's own save moves the row, the attach accounting,
   * and the budget — the announce path only covers agent edits. */
  const noteSaved = useCallback(
    (meta: {
      id: number;
      title: string;
      char_count: number;
      updated_at: string;
    }) => {
      const m = attachedRef.current;
      if (m.has(meta.id)) {
        m.set(meta.id, meta.char_count);
        syncAttached();
        // FR-51's visibility: a save that grew an ATTACHED document past
        // the budget must say so — /start would slice it silently
        const used = [...m.values()].reduce((a, b) => a + b, 0);
        if (used > MAX_TOTAL_CHARS) {
          setBudgetError(
            "this save put the attached set over the character budget — " +
              "detach something before your next session",
          );
        } else {
          // back under budget: the save-overflow warning clears itself
          // (only its own message — never a live attach refusal)
          setBudgetError((prev) =>
            prev && prev.startsWith("this save") ? null : prev,
          );
        }
      }
      setState((s) =>
        updateDocMeta(s, meta.id, meta.title, meta.char_count, meta.updated_at),
      );
    },
    [syncAttached],
  );

  /** FR-52's continuation: "show older" appends the next offset page. */
  const loadMore = useCallback(async () => {
    const res = await authedFetch(
      `/documents?offset=${stateRef.current.docs.length}`,
    );
    if (!res.ok) {
      // FR-54: a blip must not read as "there is nothing older"
      setError("couldn't load older documents — try again");
      return;
    }
    setError(null);
    const body = (await res.json()) as {
      documents: WorkspaceDoc[];
      total: number;
    };
    setTotal(body.total);
    setState((s) => {
      const known = new Set(s.docs.map((d) => d.id));
      return {
        ...s,
        docs: [...s.docs, ...body.documents.filter((d) => !known.has(d.id))],
      };
    });
  }, []);

  const upload = useCallback(
    async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      const res = await authedFetch("/documents", { method: "POST", body: form });
      if (!res.ok) {
        const detail = await res
          .json()
          .then((d) => d?.detail)
          .catch(() => null);
        throw new Error(detail || "couldn't read that file");
      }
      // FR-51 (preserved from v1): this visit's uploads enter the attach
      // set by default — upload then Talk must reach the agent with no
      // second tap. The budget refusal still guards the edge, and the
      // synchronous map sees earlier files of this same batch.
      const doc = (await res.json()) as { id: number; char_count: number };
      tryAttach(
        doc.id,
        doc.char_count,
        "uploaded, but not attached — the attach set is at its character budget",
      );
      // the batch's single refresh happens in the component's finally —
      // re-walking the whole shown corpus per file was O(pages x files)
    },
    [tryAttach],
  );

  const deleteDoc = useCallback(
    async (doc: WorkspaceDoc): Promise<boolean> => {
      const res = await authedFetch(`/documents/${doc.id}`, {
        method: "DELETE",
      }).catch(() => null);
      if (!res || !res.ok) {
        setError("couldn't delete that document");
        return false;
      }
      setState((s) => markDeleted(s, doc.id));
      // "show older" must not advertise a phantom row after a delete
      setTotal((t) => Math.max(0, t - 1));
      attachedRef.current.delete(doc.id);
      syncAttached();
      return true;
    },
    [syncAttached],
  );

  /** FR-51's visible budget refusal: adding a document that would push the
   * attach set past MAX_TOTAL_CHARS refuses with the overflow named. */
  const toggleAttach = useCallback(
    (doc: WorkspaceDoc) => {
      const m = attachedRef.current;
      if (m.has(doc.id)) {
        m.delete(doc.id);
        setError(null);
        syncAttached();
        return;
      }
      tryAttach(
        doc.id,
        doc.char_count,
        `that would put the attached set over the ${Math.round(
          MAX_TOTAL_CHARS / 1000,
        )}k-character budget — detach something first`,
      );
    },
    [syncAttached, tryAttach],
  );

  /** FR-52: download is client-composed from the fetched content — no
   * extra endpoint, nothing new to secure. */
  const download = useCallback(async (doc: WorkspaceDoc) => {
    const content = await fetchContent(doc.id);
    if (content === null) {
      setError("couldn't fetch that document");
      return;
    }
    const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = downloadFilename(doc.title, doc.format, doc.id);
    // iOS Safari (NFR-3): the anchor must be in the document, and revoking
    // in the same task as click() cancels the save
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      a.remove();
      URL.revokeObjectURL(url);
    }, 1000);
  }, []);

  return {
    state,
    attachedIds,
    attachedChars,
    total,
    loading,
    error,
    budgetError,
    refresh,
    loadMore,
    handleAnnounce,
    noteSaved,
    upload,
    deleteDoc,
    toggleAttach,
    download,
  };
}
