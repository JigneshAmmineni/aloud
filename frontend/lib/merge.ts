/**
 * FR-58's three-way merge — LINE granularity, user precedence (§4.12).
 *
 * Word-level merging was considered and rejected (stitching one sentence
 * from two authors produces fluent nonsense); overlapping line-hunks
 * conflict even when the words differ, and on conflict THE USER'S LINES
 * STAY — the explicit owner decision — with the conflict flagged so the
 * banner can offer "apply theirs".
 *
 * Pure: (base, mine, theirs) in, merged text + conflict flag out. The
 * two-way line diffs come from jsdiff; this module owns turning them
 * into base-anchored hunks and composing them.
 */

import { diffLines } from "diff";

type Hunk = {
  start: number; // first base line replaced
  delCount: number; // base lines consumed ([start, start+delCount))
  insert: string[]; // replacement lines
};

function toLines(text: string): string[] {
  // keep a trailing newline from minting a phantom last line
  const lines = text.split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}

// bound the diff: jsdiff is O(N·D) and merge3 runs on the announce path
// and on every 409 — on the same thread as a live voice session. Over
// the bound we bail to the conflict banner instead of a UI freeze.
const MAX_EDIT_LENGTH = 2000;

function hunksFrom(baseText: string, sideText: string): Hunk[] | null {
  const parts = diffLines(baseText, sideText, {
    maxEditLength: MAX_EDIT_LENGTH,
  });
  if (!parts) return null; // over the bound: the caller falls back
  const hunks: Hunk[] = [];
  let baseLine = 0;
  let open: Hunk | null = null;
  for (const part of parts) {
    const lines = toLines(part.value);
    if (part.added) {
      if (!open) open = { start: baseLine, delCount: 0, insert: [] };
      open.insert.push(...lines);
    } else if (part.removed) {
      if (!open) open = { start: baseLine, delCount: 0, insert: [] };
      open.delCount += lines.length;
      baseLine += lines.length;
    } else {
      if (open) {
        hunks.push(open);
        open = null;
      }
      baseLine += lines.length;
    }
  }
  if (open) hunks.push(open);
  return hunks;
}

function sameHunk(a: Hunk, b: Hunk): boolean {
  return (
    a.start === b.start &&
    a.delCount === b.delCount &&
    a.insert.length === b.insert.length &&
    a.insert.every((l, i) => l === b.insert[i])
  );
}

function overlaps(a: Hunk, b: Hunk): boolean {
  const aEnd = a.start + a.delCount;
  const bEnd = b.start + b.delCount;
  // half-open range intersection; two pure insertions at the same point
  // also collide (their order is ambiguous)
  if (a.start < bEnd && b.start < aEnd) return true;
  return a.delCount === 0 && b.delCount === 0 && a.start === b.start;
}

export type MergeResult = {
  merged: string;
  conflict: boolean; // some of THEIRS was dropped under user precedence
  /** the diff exceeded MAX_EDIT_LENGTH: merged is MINE wholesale, and
   * nothing hunk-level exists for "apply theirs" to flip (round 6) */
  bailed?: true;
};

/** Line-level diff3. Disjoint hunks merge (both survive); overlapping
 * hunks keep MINE and set `conflict`; identical hunks apply once. */
export function merge3(base: string, mine: string, theirs: string): MergeResult {
  if (mine === theirs) return { merged: mine, conflict: false };
  const mineHunks = hunksFrom(base, mine);
  const theirHunks = mineHunks ? hunksFrom(base, theirs) : null;
  if (!mineHunks || !theirHunks) {
    // keeping MINE with the banner is already the specified outcome
    // for "we couldn't reconcile this"
    return { merged: mine, conflict: true, bailed: true };
  }

  let conflict = false;
  const chosen: Hunk[] = [...mineHunks];
  for (const th of theirHunks) {
    const collision = mineHunks.find((mh) => overlaps(mh, th));
    if (collision) {
      if (!sameHunk(collision, th)) conflict = true; // mine stays
      continue;
    }
    chosen.push(th);
  }
  chosen.sort((a, b) => a.start - b.start || a.delCount - b.delCount);

  const baseLines = toLines(base);
  const out: string[] = [];
  let cursor = 0;
  for (const h of chosen) {
    while (cursor < h.start) out.push(baseLines[cursor++]);
    out.push(...h.insert);
    cursor += h.delCount;
  }
  while (cursor < baseLines.length) out.push(baseLines[cursor++]);

  // trailing newline follows USER precedence: if the user's side changed
  // it (deleted or added it), their choice wins; otherwise theirs' stands
  const mineChangedTrailing = mine.endsWith("\n") !== base.endsWith("\n");
  const trailing = mineChangedTrailing
    ? mine.endsWith("\n")
    : theirs.endsWith("\n");
  return {
    merged: out.join("\n") + (out.length && trailing ? "\n" : ""),
    conflict,
  };
}
