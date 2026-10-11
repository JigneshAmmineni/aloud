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

  it("flush saves a dirty buffer immediately and settles", async () => {
    let saved = 0;
    const s = createSaveScheduler(async () => {
      saved += 1;
    });
    s.noteDirty();
    await s.flush();
    expect(saved).toBe(1);
    await s.flush(); // clean: no extra save
    expect(saved).toBe(1);
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
