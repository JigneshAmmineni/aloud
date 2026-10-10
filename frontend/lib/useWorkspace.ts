"use client";

/**
 * The workspace hook (FR-54/55): server-backed document state, announce
 * upserts, attach selection with the visible budget refusal, upload,
 * delete, preview, and download. The decision logic lives in
 * lib/workspace.ts as pure functions; this hook owns the IO.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { authedFetch } from "@/lib/auth";
import {
  MAX_TOTAL_CHARS,
  attachTotal,
  downloadFilename,
  emptyWorkspace,
  markDeleted,
  rehydrate,
  setPreview,
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
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // the reducer decides WHETHER to refetch; the effect below does the IO
  const refetchIdRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await authedFetch("/documents");
      if (!res.ok) throw new Error();
      const body = (await res.json()) as {
        documents: WorkspaceDoc[];
        total: number;
      };
      setState((s) => rehydrate(s, body.documents));
      setTotal(body.total);
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

  /** Wired into the session's onServerMessage: FR-55's upsert rules. */
  const handleAnnounce = useCallback((type: string, doc: AnnouncedDoc) => {
    if (type !== "document.created" && type !== "document.updated") return;
    setState((s) => {
      const { state: next, refetch } = upsertFromAnnounce(s, doc);
      if (refetch) refetchIdRef.current = doc.id;
      return next;
    });
  }, []);

  // the refetch half (content_omitted while previewed): outside the reducer
  useEffect(() => {
    const id = refetchIdRef.current;
    if (id === null) return;
    refetchIdRef.current = null;
    void fetchContent(id).then((content) => {
      if (content !== null) {
        setState((s) => (s.previewId === id ? { ...s, previewContent: content } : s));
      }
    });
  }, [state]);

  const preview = useCallback(async (doc: WorkspaceDoc) => {
    setState((s) => setPreview(s, doc.id, null));
    const content = await fetchContent(doc.id);
    setState((s) =>
      s.previewId === doc.id ? { ...s, previewContent: content ?? "" } : s,
    );
  }, []);

  const closePreview = useCallback(() => {
    setState((s) => setPreview(s, null));
  }, []);

  /** FR-52's continuation: a migrated corpus can exceed the page the
   * first fetch returned — "show older" appends the next offset page. */
  const loadMore = useCallback(async () => {
    const res = await authedFetch(`/documents?offset=${state.docs.length}`);
    if (!res.ok) return;
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
  }, [state.docs.length]);

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
      // second tap. The budget refusal still guards the edge.
      const doc = (await res.json()) as {
        id: number;
        char_count: number;
      };
      setAttachedIds((ids) => {
        if (ids.includes(doc.id)) return ids;
        const used = attachTotal(state.docs, ids);
        if (used + doc.char_count > MAX_TOTAL_CHARS) {
          setError(
            "uploaded, but not attached — the attach set is at its " +
              "character budget",
          );
          return ids;
        }
        return [...ids, doc.id];
      });
      // FR-54: uploads surface immediately; the list itself is server truth
      await refresh();
    },
    [refresh, state.docs],
  );

  const deleteDoc = useCallback(async (doc: WorkspaceDoc) => {
    const res = await authedFetch(`/documents/${doc.id}`, { method: "DELETE" });
    if (!res.ok) {
      setError("couldn't delete that document");
      return;
    }
    setState((s) => markDeleted(s, doc.id));
    setAttachedIds((ids) => ids.filter((i) => i !== doc.id));
  }, []);

  /** FR-51's visible budget refusal: adding a document that would push the
   * attach set past MAX_TOTAL_CHARS refuses with the overflow named. */
  const toggleAttach = useCallback(
    (doc: WorkspaceDoc) => {
      setAttachedIds((ids) => {
        if (ids.includes(doc.id)) {
          setError(null);
          return ids.filter((i) => i !== doc.id);
        }
        const total = attachTotal(state.docs, ids) + doc.char_count;
        if (total > MAX_TOTAL_CHARS) {
          setError(
            `that would put the attached set over the ${Math.round(
              MAX_TOTAL_CHARS / 1000,
            )}k-character budget — detach something first`,
          );
          return ids;
        }
        setError(null);
        return [...ids, doc.id];
      });
    },
    [state.docs],
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
    total,
    loading,
    error,
    loadMore,
    handleAnnounce,
    preview,
    closePreview,
    upload,
    deleteDoc,
    toggleAttach,
    download,
  };
}
