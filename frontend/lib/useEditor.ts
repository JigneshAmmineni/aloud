"use client";

/**
 * FR-57/58: the editor pane's state — tabs (one per document, re-open
 * focuses), buffers with the opaque CAS base, the autosave scheduler per
 * tab, merge-on-announce, bounded 409 retries, and failure
 * classification. The merge and the scheduler are the pure modules; this
 * hook owns the IO and the precedence of outcomes.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { authedFetch } from "@/lib/auth";
import { createSaveScheduler, type SaveScheduler } from "@/lib/autosave";
import { merge3 } from "@/lib/merge";
import type { AnnouncedDoc, WorkspaceDoc } from "@/lib/workspace";

const MAX_409_RETRIES = 3;

export type EditorTab = {
  id: number;
  title: string;
  format: string;
  /** last content synced with the server (merge base) */
  base: string;
  /** OPAQUE CAS token — the exact string from the last 200/409/GET,
   * never parsed (FR-56: a Date round-trip would self-409 forever) */
  baseToken: string | null;
  buffer: string;
  dirty: boolean;
  saveState: "saved" | "saving" | "unsaved" | "error";
  /** terminal failure text (404/403/gate/ceiling) — autosave stopped,
   * buffer offered for download */
  terminal: string | null;
  /** conflict banner: some of the agent's edit was dropped under user
   * precedence; `theirs` is what "apply theirs" restores */
  conflict: boolean;
  theirs: string | null;
  loading: boolean;
  loadFailed: boolean;
  view: "source" | "preview";
};

type SavedMeta = { id: number; title: string; char_count: number };

export function useEditor({
  onSaved,
}: {
  /** list row + attach accounting update after the user's own save */
  onSaved?: (meta: SavedMeta) => void;
} = {}) {
  const [tabs, setTabs] = useState<EditorTab[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const schedulers = useRef<Map<number, SaveScheduler>>(new Map());
  const retries = useRef<Map<number, number>>(new Map());
  const tabsRef = useRef<EditorTab[]>([]);
  useEffect(() => {
    tabsRef.current = tabs;
  }, [tabs]);

  const patchTab = useCallback(
    (id: number, patch: Partial<EditorTab> | ((t: EditorTab) => Partial<EditorTab>)) => {
      setTabs((ts) =>
        ts.map((t) =>
          t.id === id
            ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) }
            : t,
        ),
      );
    },
    [],
  );

  const doSave = useCallback(
    async (id: number) => {
      const tab = tabsRef.current.find((t) => t.id === id);
      if (!tab || tab.terminal || tab.loading || tab.loadFailed) return;
      const sent = tab.buffer;
      const sentTitle = tab.title;
      patchTab(id, { saveState: "saving" });
      let res: Response;
      try {
        res = await authedFetch(`/documents/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            content: sent,
            base_updated_at: tab.baseToken,
            // always carried: a rename rides the same CAS statement, and
            // re-sending an unchanged title is a no-op SET
            title: sentTitle,
          }),
        });
      } catch {
        // network: retryable — keep the buffer, the next dirty reschedules
        patchTab(id, { saveState: "unsaved" });
        schedulers.current.get(id)?.noteDirty();
        return;
      }

      if (res.status === 200) {
        const body = (await res.json()) as {
          updated_at: string;
          char_count: number;
        };
        retries.current.set(id, 0);
        patchTab(id, (t) => ({
          base: sent,
          baseToken: body.updated_at,
          dirty: t.buffer !== sent,
          saveState: t.buffer !== sent ? "unsaved" : "saved",
        }));
        onSaved?.({ id, title: sentTitle, char_count: body.char_count });
        return;
      }

      if (res.status === 409) {
        const cur = (await res.json()) as {
          content: string;
          updated_at: string | null;
          title: string;
        };
        const attempt = (retries.current.get(id) ?? 0) + 1;
        retries.current.set(id, attempt);
        patchTab(id, (t) => {
          const r = merge3(t.base, t.buffer, cur.content);
          return {
            buffer: r.merged,
            base: cur.content,
            baseToken: cur.updated_at,
            title: cur.title,
            dirty: true,
            conflict: t.conflict || r.conflict,
            theirs: r.conflict ? cur.content : t.theirs,
            saveState: attempt >= MAX_409_RETRIES ? "error" : "unsaved",
            terminal: null,
          };
        });
        if (attempt < MAX_409_RETRIES) {
          // bounded retry with backoff: re-enter through the scheduler
          setTimeout(
            () => schedulers.current.get(id)?.noteDirty(),
            attempt * 1000,
          );
        }
        return;
      }

      if (res.status === 429 || res.status >= 500) {
        patchTab(id, { saveState: "unsaved" });
        schedulers.current.get(id)?.noteDirty();
        return;
      }

      // terminal: 404 (deleted elsewhere), 403, the format gate, the
      // ceiling — stop retrying, name it, keep the buffer exportable
      const detail = await res
        .json()
        .then((d) => d?.detail)
        .catch(() => null);
      schedulers.current.get(id)?.dispose();
      patchTab(id, {
        saveState: "error",
        terminal:
          res.status === 404
            ? "this document was deleted elsewhere — download your copy below"
            : detail || "this document can't be saved",
      });
    },
    [onSaved, patchTab],
  );

  const open = useCallback(
    async (doc: WorkspaceDoc) => {
      // FR-57.2: ONE tab per document — re-open focuses, never duplicates
      if (tabsRef.current.some((t) => t.id === doc.id)) {
        setActiveId(doc.id);
        return;
      }
      const tab: EditorTab = {
        id: doc.id,
        title: doc.title,
        format: doc.format,
        base: "",
        baseToken: null,
        buffer: "",
        dirty: false,
        saveState: "saved",
        terminal: null,
        conflict: false,
        theirs: null,
        loading: true,
        loadFailed: false,
        view: doc.format === "markdown" ? "preview" : "source",
      };
      setTabs((ts) => [...ts, tab]);
      setActiveId(doc.id);
      if (!schedulers.current.has(doc.id)) {
        schedulers.current.set(
          doc.id,
          createSaveScheduler(() => doSave(doc.id)),
        );
      }
      const res = await authedFetch(`/documents/${doc.id}`).catch(() => null);
      if (!res || !res.ok) {
        // FR-57.3's data-loss guard: a failed fetch must NEVER yield a
        // blank editable buffer that autosaves over the real document
        patchTab(doc.id, { loading: false, loadFailed: true });
        return;
      }
      const body = (await res.json()) as {
        content: string;
        updated_at: string | null;
        title: string;
        format: string;
      };
      patchTab(doc.id, {
        loading: false,
        base: body.content,
        buffer: body.content,
        baseToken: body.updated_at,
        title: body.title,
        format: body.format,
      });
    },
    [doSave, patchTab],
  );

  const edit = useCallback(
    (id: number, text: string) => {
      patchTab(id, { buffer: text, dirty: true, saveState: "unsaved" });
      schedulers.current.get(id)?.noteDirty();
    },
    [patchTab],
  );

  const setView = useCallback(
    (id: number, view: "source" | "preview") => {
      patchTab(id, { view });
      // leaving the source view is a flush point (FR-58.1)
      if (view === "preview") void schedulers.current.get(id)?.flush();
    },
    [patchTab],
  );

  const applyTheirs = useCallback(
    (id: number) => {
      patchTab(id, (t) => ({
        buffer: t.theirs ?? t.buffer,
        conflict: false,
        theirs: null,
        dirty: true,
        saveState: "unsaved",
      }));
      schedulers.current.get(id)?.noteDirty();
    },
    [patchTab],
  );

  const dismissConflict = useCallback(
    (id: number) => patchTab(id, { conflict: false, theirs: null }),
    [patchTab],
  );

  const close = useCallback(
    async (id: number) => {
      const tab = tabsRef.current.find((t) => t.id === id);
      if (tab && tab.dirty && !tab.terminal) {
        await schedulers.current.get(id)?.flush();
        const after = tabsRef.current.find((t) => t.id === id);
        if (after && after.dirty) return; // failed flush keeps the tab open
      }
      schedulers.current.get(id)?.dispose();
      schedulers.current.delete(id);
      retries.current.delete(id);
      setTabs((ts) => ts.filter((t) => t.id !== id));
      setActiveId((a) => {
        if (a !== id) return a;
        const rest = tabsRef.current.filter((t) => t.id !== id);
        return rest.length ? rest[rest.length - 1].id : null;
      });
    },
    [],
  );

  /** A deleted document closes its tab unconditionally (FR-57.3). */
  const closeDeleted = useCallback((id: number) => {
    schedulers.current.get(id)?.dispose();
    schedulers.current.delete(id);
    setTabs((ts) => ts.filter((t) => t.id !== id));
    setActiveId((a) => (a === id ? null : a));
  }, []);

  /** FR-58.2: agent edits reach open editors — clean buffers re-render in
   * place; dirty buffers merge (refetching `theirs` when the announce
   * omitted content, the common case past 16k). */
  const handleAnnounce = useCallback(
    (type: string, doc: AnnouncedDoc) => {
      if (type !== "document.updated" && type !== "document.created") return;
      const tab = tabsRef.current.find((t) => t.id === doc.id);
      if (!tab || tab.loading || tab.loadFailed || tab.terminal) return;

      const apply = (theirs: string, token: string | null) => {
        patchTab(doc.id, (t) => {
          if (!t.dirty) {
            return {
              base: theirs,
              buffer: theirs,
              baseToken: token,
              title: doc.title ?? t.title,
            };
          }
          const r = merge3(t.base, t.buffer, theirs);
          return {
            buffer: r.merged,
            base: theirs,
            baseToken: token,
            dirty: true,
            saveState: "unsaved",
            conflict: t.conflict || r.conflict,
            theirs: r.conflict ? theirs : t.theirs,
            title: doc.title ?? t.title,
          };
        });
        if (tab.dirty) {
          // the merged buffer still needs saving
          schedulers.current.get(doc.id)?.noteDirty();
        }
      };

      const token = (doc as { updated_at?: string | null }).updated_at ?? null;
      if (typeof doc.content === "string" && !doc.content_omitted) {
        apply(doc.content, token);
        return;
      }
      // omitted: the pending autosave HOLDS (nothing schedules) until this
      // refetch lands, then the merge runs — one round trip, not two
      void authedFetch(`/documents/${doc.id}`)
        .then((r) => (r.ok ? r.json() : null))
        .then(
          (body: { content: string; updated_at: string | null } | null) => {
            if (body) apply(body.content, body.updated_at);
          },
        );
    },
    [patchTab],
  );

  /** FR-58.1: the pre-/start flush — all dirty tabs, success required
   * (a dirty terminal tab blocks too: the user resolves it explicitly,
   * by downloading/discarding, before the agent reads stale state). */
  const flushAll = useCallback(async (): Promise<boolean> => {
    for (const [, s] of schedulers.current) await s.flush();
    return tabsRef.current.every((t) => !t.dirty);
  }, []);

  const rename = useCallback(
    (id: number, title: string) => {
      patchTab(id, { title, dirty: true, saveState: "unsaved" });
      schedulers.current.get(id)?.noteDirty();
    },
    [patchTab],
  );

  return {
    tabs,
    activeId,
    setActiveId,
    open,
    edit,
    setView,
    close,
    closeDeleted,
    applyTheirs,
    dismissConflict,
    handleAnnounce,
    flushAll,
    rename,
  };
}
