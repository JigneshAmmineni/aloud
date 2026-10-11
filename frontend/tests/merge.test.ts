/**
 * FR-58's merge module, mapped to ROADMAP's six race cases (the spec's
 * checklist). Line granularity; user precedence on conflict.
 */
import { describe, expect, it } from "vitest";

import { merge3 } from "@/lib/merge";

const base = "alpha\nbravo\ncharlie\ndelta\necho\n";

describe("merge3 (line-level diff3, user precedence)", () => {
  it("case 3 clean half: disjoint hunks — both edits survive", () => {
    const mine = "alpha EDITED\nbravo\ncharlie\ndelta\necho\n";
    const theirs = "alpha\nbravo\ncharlie\ndelta\necho AGENT\n";
    const r = merge3(base, mine, theirs);
    expect(r.conflict).toBe(false);
    expect(r.merged).toBe("alpha EDITED\nbravo\ncharlie\ndelta\necho AGENT\n");
  });

  it("case 3 banner half / cases 4 & 6: overlapping hunks keep MINE and flag", () => {
    const mine = "alpha\nbravo MINE\ncharlie\ndelta\necho\n";
    const theirs = "alpha\nbravo THEIRS\ncharlie\ndelta\necho\n";
    const r = merge3(base, mine, theirs);
    expect(r.conflict).toBe(true);
    expect(r.merged).toContain("bravo MINE");
    expect(r.merged).not.toContain("THEIRS");
  });

  it("same line, different WORDS still conflicts — word-level stitching rejected", () => {
    const mine = "alpha\nbravo plus mine\ncharlie\ndelta\necho\n";
    const theirs = "alpha\nbravo plus theirs\ncharlie\ndelta\necho\n";
    expect(merge3(base, mine, theirs).conflict).toBe(true);
  });

  it("identical hunks apply once, no conflict", () => {
    const both = "alpha\nbravo SAME\ncharlie\ndelta\necho\n";
    const r = merge3(base, both, both);
    expect(r).toEqual({ merged: both, conflict: false });
  });

  it("insertions at different points both land; same point conflicts", () => {
    const mine = "alpha\nNEW MINE\nbravo\ncharlie\ndelta\necho\n";
    const theirsFar = "alpha\nbravo\ncharlie\ndelta\nNEW THEIRS\necho\n";
    const ok = merge3(base, mine, theirsFar);
    expect(ok.conflict).toBe(false);
    expect(ok.merged).toContain("NEW MINE");
    expect(ok.merged).toContain("NEW THEIRS");

    const theirsSame = "alpha\nNEW THEIRS\nbravo\ncharlie\ndelta\necho\n";
    const clash = merge3(base, mine, theirsSame);
    expect(clash.conflict).toBe(true);
    expect(clash.merged).toContain("NEW MINE");
    expect(clash.merged).not.toContain("NEW THEIRS");
  });

  it("deletion vs edit of the same region conflicts, user's deletion wins", () => {
    const mine = "alpha\ncharlie\ndelta\necho\n"; // user deleted bravo
    const theirs = "alpha\nbravo THEIRS\ncharlie\ndelta\necho\n";
    const r = merge3(base, mine, theirs);
    expect(r.conflict).toBe(true);
    expect(r.merged).not.toContain("bravo");
  });

  it("theirs-only changes apply wholesale on a clean-ish buffer", () => {
    const theirs = "alpha\nbravo\nREWRITTEN\ndelta\necho\n";
    const r = merge3(base, base, theirs);
    expect(r).toEqual({ merged: theirs, conflict: false });
  });

  it("no trailing-newline phantom lines", () => {
    const r = merge3("a\nb", "a\nb\nc", "a EDIT\nb");
    expect(r.conflict).toBe(false);
    expect(r.merged).toBe("a EDIT\nb\nc");
  });
});

describe("trailing newline follows user precedence", () => {
  it("a user-deleted trailing newline stays deleted through a merge", () => {
    const r = merge3("a\nb\n", "a\nb", "a EDIT\nb\n");
    expect(r.merged).toBe("a EDIT\nb");
  });

  it("an untouched trailing newline follows theirs", () => {
    const r = merge3("a\nb\n", "a\nb\n", "a\nb");
    expect(r.merged).toBe("a\nb");
  });
});

describe("the edit bound", () => {
  it("a diff too large to reconcile bails to mine + banner, not a freeze", () => {
    const n = 3000;
    const b = Array.from({ length: n }, (_, i) => `line ${i}`).join("\n");
    const m = Array.from({ length: n }, (_, i) => `mine ${i}`).join("\n");
    const t = Array.from({ length: n }, (_, i) => `theirs ${i}`).join("\n");
    expect(merge3(b, m, t)).toEqual({ merged: m, conflict: true });
  });
});
