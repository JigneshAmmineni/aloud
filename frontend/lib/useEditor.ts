"use client";

/**
 * FR-57/58: the editor pane's state — tabs (one per document, re-open
 * focuses), buffers with the opaque CAS base, the autosave scheduler per
 * tab, merge-on-announce, bounded 409 retries with backoff, and failure
 * classification. The merge and the scheduler are the pure modules; this
 * hook owns the IO and the precedence of outcomes.
 *
 * State discipline: every mutation goes REF-FIRST through setTabsSync —
 * tabsRef is the synchronous source event handlers read (flushAll,
 * close), and React state is its render mirror. Reading a ref that only
 * updates in an effect was round-1's root-cause bug: a successful flush
 * still reported dirty and Talk refused after a 200.
 */

import { useCallback, useRef, useState } from "react";

import { authedFetch } from "@/lib/auth";
import { createSaveScheduler, type SaveScheduler } from "@/lib/autosave";
import { merge3 } from "@/lib/merge";
import type { AnnouncedDoc, WorkspaceDoc } from "@/lib/workspace";

const MAX_409_RETRIES = 3;
const BACKOFF_CAP_MS = 30_000;

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
  /** FR-58.3: the bounded 409 retries ran out — "someone else is editing
   * this document", manual retry, buffer intact */
  stalled: boolean;
  /** conflict banner: some of the agent's edit was dropped under user
   * precedence */
  conflict: boolean;
  loading: boolean;
  loadFailed: boolean;
  view: "source" | "preview";
};

/** What "apply theirs" needs to flip precedence WITHOUT discarding the
 * user's cleanly-merged hunks (round-1 blocker 2). */
type ConflictSnap = {
  base: string; // the merge base at conflict time
  mine: string; // the user's buffer at conflict time
  theirs: string; // the server/agent content at conflict time
  merged: string; // the user-precedence result we put in the buffer
};

type SavedMeta = {
  id: number;
  title: string;
  char_count: number;
  updated_at: string;
};

export function useEditor({
  onSaved,
}: {
  /** list row + attach accounting update after the user's own save */
  onSaved?: (meta: SavedMeta) => void;
} = {}) {
  const [tabs, setTabs] = useState<EditorTab[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const tabsRef = useRef<EditorTab[]>([]);
  const schedulers = useRef<Map<number, SaveScheduler>>(new Map());
  const retries409 = useRef<Map<number, number>>(new Map());
  const failStreak = useRef<Map<number, number>>(new Map());
  const conflictSnaps = useRef<Map<number, ConflictSnap>>(new Map());

  const setTabsSync = useCallback((next: EditorTab[]) => {
    tabsRef.current = next; // ref first: event handlers read truth NOW
    setTabs(next);
  }, []);

  const patchTab = useCallback(
    (
      id: number,
      patch: Partial<EditorTab> | ((t: EditorTab) => Partial<EditorTab>),
    ) => {
      setTabsSync(
        tabsRef.current.map((t) =>
          t.id === id
            ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) }
            : t,
        ),
      );
    },
    [setTabsSync],
  );

  const dropBookkeeping = useCallback((id: number) => {
    schedulers.current.get(id)?.dispose();
    schedulers.current.delete(id);
    retries409.current.delete(id);
    failStreak.current.delete(id);
    conflictSnaps.current.delete(id);
  }, []);

  const scheduleRetry = useCallback((id: number) => {
    // FR-58.1: retryable failures back off — a flat 1 Hz loop would
    // self-inflict the save limiter and rewrite TOAST forever
    const streak = (failStreak.current.get(id) ?? 0) + 1;
    failStreak.current.set(id, streak);
    const delay = Math.min(BACKOFF_CAP_MS, 1000 * 2 ** streak);
    setTimeout(() => schedulers.current.get(id)?.noteDirty(), delay);
  }, []);

  const doSave = useCallback(
    async (id: number) => {
      const tab = tabsRef.current.find((t) => t.id === id);
      if (!tab || tab.terminal || tab.loading || tab.loadFailed || tab.stalled)
        return;
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
        patchTab(id, { saveState: "unsaved" });
        scheduleRetry(id);
        return;
      }

      if (res.status === 200) {
        const body = (await res.json()) as {
          updated_at: string;
          char_count: number;
        };
        retries409.current.set(id, 0);
        failStreak.current.set(id, 0);
        patchTab(id, (t) => ({
          base: sent,
          baseToken: body.updated_at,
          dirty: t.buffer !== sent,
          saveState: t.buffer !== sent ? "unsaved" : "saved",
        }));
        onSaved?.({
          id,
          title: sentTitle,
          char_count: body.char_count,
          updated_at: body.updated_at,
        });
        return;
      }

      if (res.status === 409) {
        const cur = (await res.json()) as {
          content: string;
          updated_at: string | null;
          title: string;
        };
        const attempt = (retries409.current.get(id) ?? 0) + 1;
        retries409.current.set(id, attempt);
        patchTab(id, (t) => {
          const r = merge3(t.base, t.buffer, cur.content);
          if (r.conflict && !conflictSnaps.current.has(id)) {
            conflictSnaps.current.set(id, {
              base: t.base,
              mine: t.buffer,
              theirs: cur.content,
              merged: r.merged,
            });
          }
          return {
            buffer: r.merged,
            base: cur.content,
            baseToken: cur.updated_at,
            title: cur.title,
            dirty: true,
            conflict: t.conflict || r.conflict,
            saveState: attempt >= MAX_409_RETRIES ? "error" : "unsaved",
            stalled: attempt >= MAX_409_RETRIES,
          };
        });
        if (attempt < MAX_409_RETRIES) {
          setTimeout(
            () => schedulers.current.get(id)?.noteDirty(),
            attempt * 1000,
          );
        }
        return;
      }

      if (res.status === 429 || res.status >= 500) {
        patchTab(id, { saveState: "unsaved" });
        scheduleRetry(id);
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
    [onSaved, patchTab, scheduleRetry],
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
        stalled: false,
        conflict: false,
        loading: true,
        loadFailed: false,
        view: doc.format === "markdown" ? "preview" : "source",
      };
      setTabsSync([...tabsRef.current, tab]);
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
    [doSave, patchTab, setTabsSync],
  );

  const edit = useCallback(
    (id: number, text: string) => {
      patchTab(id, { buffer: text, dirty: true, saveState: "unsaved" });
      schedulers.current.get(id)?.noteDirty();
    },
    [patchTab],
  );

  const rename = useCallback(
    (id: number, title: string) => {
      if (!title.trim()) return; // the server would 400; keep the old name
      patchTab(id, { title, dirty: true, saveState: "unsaved" });
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

  /** FR-58.1: switching tabs is a flush point. */
  const activate = useCallback(
    (id: number) => {
      const prev = activeId;
      setActiveId(id);
      if (prev !== null && prev !== id) {
        void schedulers.current.get(prev)?.flush();
      }
    },
    [activeId],
  );

  /** "Apply theirs" flips precedence for the CONFLICTED regions only —
   * the user's cleanly-merged hunks, and anything typed since the
   * banner appeared, survive (round-1 blocker 2). */
  const applyTheirs = useCallback(
    (id: number) => {
      const snap = conflictSnaps.current.get(id);
      conflictSnaps.current.delete(id);
      patchTab(id, (t) => {
        if (!snap) return { conflict: false };
        // theirs-precedence version of the original three-way
        const theirsWon = merge3(snap.base, snap.theirs, snap.mine).merged;
        // fold in anything the user typed after the banner appeared
        const final = merge3(snap.merged, t.buffer, theirsWon).merged;
        return {
          buffer: final,
          conflict: false,
          dirty: true,
          saveState: "unsaved",
        };
      });
      schedulers.current.get(id)?.noteDirty();
    },
    [patchTab],
  );

  const dismissConflict = useCallback(
    (id: number) => {
      conflictSnaps.current.delete(id);
      patchTab(id, { conflict: false });
    },
    [patchTab],
  );

  /** FR-58.3's manual retry out of the stalled state. */
  const retryStalled = useCallback(
    (id: number) => {
      retries409.current.set(id, 0);
      failStreak.current.set(id, 0);
      patchTab(id, { stalled: false, saveState: "unsaved" });
      schedulers.current.get(id)?.noteDirty();
    },
    [patchTab],
  );

  const close = useCallback(
    async (id: number) => {
      const tab = tabsRef.current.find((t) => t.id === id);
      if (tab && tab.dirty && !tab.terminal && !tab.stalled) {
        await schedulers.current.get(id)?.flush();
        const after = tabsRef.current.find((t) => t.id === id);
        if (after && after.dirty) return; // failed flush keeps the tab open
      }
      dropBookkeeping(id);
      const rest = tabsRef.current.filter((t) => t.id !== id);
      setTabsSync(rest);
      setActiveId((a) =>
        a === id ? (rest.length ? rest[rest.length - 1].id : null) : a,
      );
    },
    [dropBookkeeping, setTabsSync],
  );

  /** A deleted document closes its tab unconditionally (FR-57.3). */
  const closeDeleted = useCallback(
    (id: number) => {
      dropBookkeeping(id);
      const rest = tabsRef.current.filter((t) => t.id !== id);
      setTabsSync(rest);
      setActiveId((a) => (a === id ? null : a));
    },
    [dropBookkeeping, setTabsSync],
  );

  /** FR-58.2: agent edits reach open editors — clean buffers re-render in
   * place; dirty buffers merge (refetching `theirs` when the announce
   * omitted content, with the pending autosave HELD until it lands). */
  const handleAnnounce = useCallback(
    (type: string, doc: AnnouncedDoc) => {
      if (type !== "document.updated" && type !== "document.created") return;
      const tab = tabsRef.current.find((t) => t.id === doc.id);
      if (!tab || tab.loading || tab.loadFailed || tab.terminal) return;

      const apply = (theirs: string, token: string | null) => {
        const wasDirty =
          tabsRef.current.find((t) => t.id === doc.id)?.dirty ?? false;
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
          if (r.conflict && !conflictSnaps.current.has(doc.id)) {
            conflictSnaps.current.set(doc.id, {
              base: t.base,
              mine: t.buffer,
              theirs,
              merged: r.merged,
            });
          }
          return {
            buffer: r.merged,
            base: theirs,
            baseToken: token,
            dirty: true,
            saveState: "unsaved",
            conflict: t.conflict || r.conflict,
            title: doc.title ?? t.title,
          };
        });
        if (wasDirty) {
          schedulers.current.get(doc.id)?.noteDirty();
        }
      };

      const token = (doc as { updated_at?: string | null }).updated_at ?? null;
      if (typeof doc.content === "string" && !doc.content_omitted) {
        apply(doc.content, token);
        return;
      }
      // omitted: the pending autosave HOLDS until the refetch lands (one
      // round trip, not a burned 409 retry) — pause, fetch, apply, resume
      const scheduler = schedulers.current.get(doc.id);
      scheduler?.pause();
      void authedFetch(`/documents/${doc.id}`)
        .then((r) => (r.ok ? r.json() : null))
        .then(
          (body: { content: string; updated_at: string | null } | null) => {
            if (body) apply(body.content, body.updated_at);
          },
        )
        .finally(() => scheduler?.resume());
    },
    [patchTab],
  );

  /** FR-58.1: the pre-/start flush — all dirty tabs must land, including
   * stalled/terminal ones (the user resolves those explicitly before the
   * agent reads state they believe is current). */
  const flushAll = useCallback(async (): Promise<boolean> => {
    for (const [, s] of schedulers.current) await s.flush();
    return tabsRef.current.every((t) => !t.dirty);
  }, []);

  return {
    tabs,
    activeId,
    activate,
    open,
    edit,
    rename,
    setView,
    close,
    closeDeleted,
    applyTheirs,
    dismissConflict,
    retryStalled,
    handleAnnounce,
    flushAll,
  };
}
