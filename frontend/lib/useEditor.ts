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
import {
  TITLE_MAX_CHARS,
  type AnnouncedDoc,
  type WorkspaceDoc,
} from "@/lib/workspace";

const MAX_409_RETRIES = 3;
const BACKOFF_CAP_MS = 30_000;

export type EditorTab = {
  id: number;
  title: string;
  /** the last title synced with the server — the title is SENT only when
   * it differs (a legacy over-cap stored title must not brick saving),
   * and title dirtiness counts toward tab dirtiness */
  baseTitle: string;
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
  /** "gone" = the row no longer exists (survives discard); "other"
   * clears when the user discards (the pre-failure content IS saved
   * content). Never branch on the user-facing prose. */
  terminalKind: "gone" | "other" | null;
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
      if (
        !tab ||
        !tab.dirty || // flush fires unconditionally; clean tabs no-op
        tab.terminal ||
        tab.loading ||
        tab.loadFailed ||
        tab.stalled
      )
        return;
      const sent = tab.buffer;
      const sentTitle = tab.title;
      const sentToken = tab.baseToken;
      const renaming = sentTitle !== tab.baseTitle;
      patchTab(id, { saveState: "saving" });
      let res: Response;
      try {
        res = await authedFetch(`/documents/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            content: sent,
            base_updated_at: tab.baseToken,
            // only an actual rename rides along — re-sending a legacy
            // over-cap stored title would 400 every save forever
            ...(renaming ? { title: sentTitle } : {}),
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
        patchTab(id, (t) => {
          if (t.baseToken !== sentToken) {
            // an announce moved base/baseToken while this save was in
            // flight (HTTP vs data channel — the ordering isn't ours to
            // assume): adopting OUR result would roll truth backwards
            // and guarantee a 409 against a stale base. Keep the newer
            // base; our content is still unsynced relative to it.
            const stillDirty = t.buffer !== t.base || t.title !== t.baseTitle;
            return {
              dirty: stillDirty,
              saveState: stillDirty ? "unsaved" : "saved",
            };
          }
          // a rename issued DURING this save stays dirty (round-3: the
          // buffer-only recompute dropped it with a lying "saved" chip)
          const stillDirty = t.buffer !== sent || t.title !== sentTitle;
          return {
            base: sent,
            baseTitle: sentTitle,
            baseToken: body.updated_at,
            dirty: stillDirty,
            saveState: stillDirty ? "unsaved" : "saved",
          };
        });
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
          if (r.conflict) {
            // a later conflict refreshes theirs/merged so "apply theirs"
            // replays the NEWEST agent edit, not the first one
            const prev = conflictSnaps.current.get(id);
            conflictSnaps.current.set(id, {
              base: prev?.base ?? t.base,
              mine: prev?.mine ?? t.buffer,
              theirs: cur.content,
              merged: r.merged,
            });
          }
          return {
            buffer: r.merged,
            base: cur.content,
            // user precedence covers renames too: a local rename
            // survives the 409; otherwise the server's title is truth
            title: t.title !== t.baseTitle ? t.title : cur.title,
            baseTitle: cur.title,
            baseToken: cur.updated_at,
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

      if (res.status === 401 || res.status === 429 || res.status >= 500) {
        // 401 is RETRYABLE (FR-58.1 "401-then-refresh"): authedFetch sends
        // no Bearer while auth.currentUser is momentarily null — terminal
        // here would freeze the editor read-only mid-sentence
        patchTab(id, { saveState: "unsaved" });
        scheduleRetry(id);
        return;
      }

      const errBody = (await res.json().catch(() => null)) as {
        detail?: string;
        code?: string;
      } | null;
      const detail = errBody?.detail ?? null;
      // the CODE, not the prose: branching on the detail text was the
      // terminalKind fragility one classifier down (round 5)
      if (res.status === 400 && errBody?.code === "title_invalid") {
        // a title-validation 400 is the one 4xx the user can fix in
        // place — and NOT a member of FR-58.1's TERMINAL class. Revert
        // the rename; the content edits it rode along with keep saving.
        patchTab(id, (t) => ({
          title: t.baseTitle,
          saveState: "unsaved",
        }));
        schedulers.current.get(id)?.noteDirty();
        return;
      }
      // terminal: 404 (deleted elsewhere), 403, the format gate, the
      // ceiling — stop retrying, name it, keep the buffer exportable
      schedulers.current.get(id)?.dispose();
      patchTab(id, {
        saveState: "error",
        terminalKind: res.status === 404 ? "gone" : "other",
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
        baseTitle: doc.title,
        format: doc.format,
        base: "",
        baseToken: null,
        buffer: "",
        dirty: false,
        saveState: "saved",
        terminal: null,
        terminalKind: null,
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
        baseTitle: body.title,
        format: body.format,
      });
    },
    [doSave, patchTab, setTabsSync],
  );

  const edit = useCallback(
    (id: number, text: string) => {
      const current = tabsRef.current.find((t) => t.id === id);
      if (!current || current.buffer === text) {
        // the echo of our own external dispatch (announce merge,
        // discard) — re-dirtying here broke discard-on-terminal and
        // re-PUT every clean announce (round-3 blocker 2)
        return;
      }
      patchTab(id, (t) => ({
        buffer: text,
        dirty: true,
        // a stalled/terminal tab keeps its error chip — "unsaved" would
        // quietly promise an autosave that is not coming
        saveState: t.stalled || t.terminal ? t.saveState : "unsaved",
      }));
      schedulers.current.get(id)?.noteDirty();
    },
    [patchTab],
  );

  const rename = useCallback(
    (id: number, title: string) => {
      // over-cap/empty would 400 server-side; keep the old name (the
      // title input is capped to the same constant)
      if (!title.trim() || title.length > TITLE_MAX_CHARS) return;
      patchTab(id, (t) => ({
        title,
        dirty: true,
        saveState: t.stalled || t.terminal ? t.saveState : "unsaved",
      }));
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

  /** "Discard changes": revert the buffer to the last synced content —
   * the explicit way out of a stuck save (failed flush, stalled,
   * terminal) that the Talk-refusal copy promises. The tab stays open;
   * the user loses only what could not be saved, by their own hand. */
  const discard = useCallback(
    (id: number) => {
      retries409.current.set(id, 0);
      failStreak.current.set(id, 0);
      conflictSnaps.current.delete(id);
      patchTab(id, (t) => ({
        buffer: t.base,
        title: t.baseTitle, // a doomed rename reverts with the content
        dirty: false,
        saveState: "saved",
        stalled: false,
        conflict: false,
        // a gone-terminal stays terminal (the row no longer exists);
        // gate/ceiling terminals clear — pre-failure content IS saved
        terminal: t.terminalKind === "gone" ? t.terminal : null,
        terminalKind: t.terminalKind === "gone" ? t.terminalKind : null,
      }));
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
          const titleDirty = t.title !== t.baseTitle;
          if (!t.dirty) {
            return {
              base: theirs,
              buffer: theirs,
              baseToken: token,
              title: titleDirty ? t.title : (doc.title ?? t.title),
              baseTitle: doc.title ?? t.baseTitle,
            };
          }
          const r = merge3(t.base, t.buffer, theirs);
          if (r.conflict) {
            const prev = conflictSnaps.current.get(doc.id);
            conflictSnaps.current.set(doc.id, {
              base: prev?.base ?? t.base,
              mine: prev?.mine ?? t.buffer,
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
            title: titleDirty ? t.title : (doc.title ?? t.title),
            baseTitle: doc.title ?? t.baseTitle,
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
        .catch(() => null) // offline: no unhandled rejection; resume below
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
    discard,
    handleAnnounce,
    flushAll,
  };
}
