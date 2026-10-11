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

  const clearTimers = () => {
    if (debounceTimer) clearTimeout(debounceTimer);
    if (maxWaitTimer) clearTimeout(maxWaitTimer);
    debounceTimer = null;
    maxWaitTimer = null;
  };

  const fire = async () => {
    clearTimers();
    if (!dirty || paused) return;
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
    async flush() {
      if (inflight) await inflight;
      // save UNCONDITIONALLY: the scheduler's flag clears before a save,
      // so after a FAILED save only the caller's state still knows the
      // buffer is dirty — the saver no-ops when clean (round-2 blocker 1)
      paused = false; // an explicit flush point overrides the hold
      clearTimers();
      dirty = false;
      inflight = save().catch(() => {});
      await inflight;
      inflight = null;
    },
    dispose() {
      clearTimers();
      dirty = false;
    },
  };
}
