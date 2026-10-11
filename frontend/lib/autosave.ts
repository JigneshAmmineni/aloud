/**
 * FR-58.1's save scheduler: a trailing debounce PAIRED with a hard
 * max-wait while dirty — the debounce alone fires only on a typing gap,
 * so continuous typing would leave the server minutes behind; the
 * max-wait is the dirty-window ceiling, the debounce just keeps quiet
 * moments cheap. Pure scheduling (timers in, one save callback out);
 * failure classification lives with the caller.
 */

export const AUTOSAVE_DEBOUNCE_MS = 1000;
export const AUTOSAVE_MAX_WAIT_MS = 3000;

export type SaveScheduler = {
  noteDirty(): void;
  /** Save now if dirty; resolves when the in-flight save settles. */
  flush(): Promise<void>;
  /** FR-58.2's hold: timers stop (dirtiness is kept) until resume —
   * the pending autosave must not race the omitted-content refetch. */
  pause(): void;
  resume(): void;
  dispose(): void;
};

export function createSaveScheduler(
  save: () => Promise<void>,
  {
    debounceMs = AUTOSAVE_DEBOUNCE_MS,
    maxWaitMs = AUTOSAVE_MAX_WAIT_MS,
  }: { debounceMs?: number; maxWaitMs?: number } = {},
): SaveScheduler {
  let debounceTimer: ReturnType<typeof setTimeout> | null = null;
  let maxWaitTimer: ReturnType<typeof setTimeout> | null = null;
  let dirty = false;
  let paused = false;
  let inflight: Promise<void> | null = null;
  let flushing: Promise<void> | null = null;

  const clearTimers = () => {
    if (debounceTimer) clearTimeout(debounceTimer);
    if (maxWaitTimer) clearTimeout(maxWaitTimer);
    debounceTimer = null;
    maxWaitTimer = null;
  };

  const fire = async () => {
    clearTimers();
    // ONE save at a time: a keystroke during a slow save must not launch
    // a sibling PUT with the same base (it would 409 against itself,
    // burn a retry slot, and fake an agent conflict — round 4 blocker 1);
    // the in-flight save's finally reschedules for the new dirtiness.
    if (inflight || !dirty || paused) return;
    dirty = false;
    inflight = save()
      .catch(() => {}) // the saver owns failure handling; never leak
      .finally(() => {
        inflight = null;
        // keystrokes during the save re-dirtied: reschedule
        if (dirty && !debounceTimer) {
          debounceTimer = setTimeout(fire, debounceMs);
          maxWaitTimer = setTimeout(fire, maxWaitMs);
        }
      });
    await inflight;
  };

  return {
    noteDirty() {
      dirty = true;
      if (paused) return; // held: dirtiness kept, timers stay quiet
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(fire, debounceMs);
      // the ceiling: set once per dirty stretch, never reset by keystrokes
      if (!maxWaitTimer) maxWaitTimer = setTimeout(fire, maxWaitMs);
    },
    pause() {
      paused = true;
      clearTimers();
    },
    resume() {
      paused = false;
      if (dirty) {
        debounceTimer = setTimeout(fire, debounceMs);
        maxWaitTimer = setTimeout(fire, maxWaitMs);
      }
    },
    flush() {
      // ONE flush at a time too: two flush points landing while a save
      // is in flight (preview toggle + tab switch on the same tab) would
      // both park on inflight, then both start sibling PUTs with the
      // same base — a self-409 that burns a retry slot (round 6).
      if (flushing) return flushing;
      flushing = (async () => {
        if (inflight) await inflight;
      // save UNCONDITIONALLY: the scheduler's flag clears before a save,
      // so after a FAILED save only the caller's state still knows the
      // buffer is dirty — the saver no-ops when clean (round-2 blocker 1).
      // The hold is OVERRIDDEN for this save but RESTORED after: a flush
      // point inside the omitted-content refetch window must not kill
      // FR-58.2's hold for the tab's lifetime (round 3).
      const wasPaused = paused;
      paused = false;
      clearTimers();
      dirty = false;
      inflight = save()
        .catch(() => {})
        .finally(() => {
          inflight = null;
          paused = wasPaused;
          // a keystroke during THIS save re-dirtied, and its fire()
          // bailed on the in-flight guard with the timers already
          // cleared — without this re-arm the buffer sits dirty with
          // no ceiling until the next keystroke (round-5 blocker 1)
          if (dirty && !debounceTimer && !paused) {
            debounceTimer = setTimeout(fire, debounceMs);
            maxWaitTimer = setTimeout(fire, maxWaitMs);
          }
        });
        await inflight;
      })().finally(() => {
        flushing = null;
      });
      return flushing;
    },
    dispose() {
      clearTimers();
      dirty = false;
    },
  };
}
