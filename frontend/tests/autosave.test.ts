/** FR-58.1's scheduler: the debounce fires on a gap, the MAX-WAIT is the
 * ceiling under continuous typing, and flush settles the dirty buffer. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createSaveScheduler } from "@/lib/autosave";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("createSaveScheduler", () => {
  it("case 1: the debounce fires after a typing gap", async () => {
    const saves: number[] = [];
    const s = createSaveScheduler(async () => {
      saves.push(Date.now());
    });
    s.noteDirty();
    await vi.advanceTimersByTimeAsync(999);
    expect(saves).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1);
    expect(saves).toHaveLength(1);
  });

  it("a continuously-dirty buffer saves at least once per max-wait", async () => {
    const saves: number[] = [];
    const s = createSaveScheduler(async () => {
      saves.push(Date.now());
    });
    // keystrokes every 500ms forever: the debounce never gets its gap
    for (let t = 0; t < 12; t++) {
      s.noteDirty();
      await vi.advanceTimersByTimeAsync(500);
    }
    // 6s of continuous typing with a 3s max-wait: at least two saves
    expect(saves.length).toBeGreaterThanOrEqual(2);
  });

  it("flush invokes the saver unconditionally — the saver owns dirtiness", async () => {
    // round-2 blocker 1: the scheduler's own flag clears BEFORE a save,
    // so a failed save left flush() a no-op while the buffer stayed
    // dirty; flush now always calls through and the saver no-ops when
    // clean (useEditor's doSave guards on the tab's dirty flag)
    let saved = 0;
    const s = createSaveScheduler(async () => {
      saved += 1;
    });
    s.noteDirty();
    await s.flush();
    expect(saved).toBe(1);
    await s.flush(); // called again; a clean SAVER no-ops, not the scheduler
    expect(saved).toBe(2);
  });

  it("keystrokes during an in-flight save reschedule instead of dropping", async () => {
    let resolveSave: () => void = () => {};
    const saves: number[] = [];
    const s = createSaveScheduler(async () => {
      saves.push(Date.now());
      await new Promise<void>((r) => {
        resolveSave = r;
      });
    });
    s.noteDirty();
    await vi.advanceTimersByTimeAsync(1000); // save 1 starts, hangs
    s.noteDirty(); // typed while saving
    resolveSave();
    await vi.advanceTimersByTimeAsync(3000);
    expect(saves.length).toBe(2);
  });
});

describe("pause/resume (FR-58.2's hold)", () => {
  it("holds timers while paused, re-arms on resume, and flush restores the hold", async () => {
    const saves: number[] = [];
    const s = createSaveScheduler(async () => {
      saves.push(Date.now());
    });
    s.noteDirty();
    s.pause();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(saves).toHaveLength(0); // held through debounce AND max-wait

    // an explicit flush saves through the hold but RESTORES it
    await s.flush();
    expect(saves).toHaveLength(1);
    s.noteDirty();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(saves).toHaveLength(1); // still held

    s.resume();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(saves).toHaveLength(2); // re-armed on resume

    // dispose is a flag-clear, NOT a latch: discard() disposes while the
    // tab can become editable again, so later dirtiness must still save
    s.dispose();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(saves).toHaveLength(2); // dispose dropped the pending dirtiness
    s.noteDirty();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(saves).toHaveLength(3); // post-dispose dirtiness still saves
  });
});

describe("one save at a time", () => {
  it("a keystroke during a slow save never launches a sibling save", async () => {
    let resolveSave: () => void = () => {};
    const starts: number[] = [];
    const s = createSaveScheduler(async () => {
      starts.push(Date.now());
      await new Promise<void>((r) => {
        resolveSave = r;
      });
    });
    s.noteDirty();
    await vi.advanceTimersByTimeAsync(1000); // save 1 starts, hangs
    expect(starts).toHaveLength(1);
    s.noteDirty(); // keystroke mid-save re-arms the debounce
    await vi.advanceTimersByTimeAsync(5000); // debounce AND max-wait fire
    expect(starts).toHaveLength(1); // …but no concurrent sibling PUT
    resolveSave();
    await vi.advanceTimersByTimeAsync(5000);
    expect(starts).toHaveLength(2); // the reschedule lands after it settles
  });
});

describe("flush never strands the ceiling (round-5 blocker 1)", () => {
  it("a keystroke during a slow flush still saves by the max-wait", async () => {
    let slow = true;
    let resolveSave: () => void = () => {};
    const starts: number[] = [];
    const s = createSaveScheduler(async () => {
      starts.push(Date.now());
      if (slow) {
        await new Promise<void>((r) => {
          resolveSave = r;
        });
      }
    });
    s.noteDirty();
    const flushP = s.flush(); // the flush's save starts and hangs
    await vi.advanceTimersByTimeAsync(0);
    expect(starts).toHaveLength(1);
    s.noteDirty(); // typed 10ms into the flush
    await vi.advanceTimersByTimeAsync(1000); // fire() bails on in-flight
    expect(starts).toHaveLength(1);
    slow = false;
    resolveSave(); // the flush settles with the buffer dirty again
    await flushP;
    // the failure mode: dirty, no timers, nothing in flight — the buffer
    // never saved again until the NEXT keystroke or flush point
    await vi.advanceTimersByTimeAsync(5000);
    expect(starts).toHaveLength(2);
  });
});
