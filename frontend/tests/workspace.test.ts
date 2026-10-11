/**
 * FR-55's list reducer (slimmed by §4.12: open-document behavior lives in
 * the editor hook; the reducer owns the LIST — upserts, tombstones,
 * rehydration, the user's-save meta update) plus sorting and the
 * download filename.
 */
import { describe, expect, it } from "vitest";

import {
  downloadFilename,
  emptyWorkspace,
  markDeleted,
  nextSort,
  rehydrate,
  sortDocs,
  updateDocMeta,
  upsertFromAnnounce,
  type AnnouncedDoc,
  type WorkspaceDoc,
} from "@/lib/workspace";

const doc = (over: Partial<WorkspaceDoc> & { id: number }): WorkspaceDoc => ({
  title: `d${over.id}`,
  kind: null,
  format: "text",
  source: "uploaded",
  char_count: 10,
  ...over,
});

describe("upsertFromAnnounce", () => {
  it("inserts an unknown id carrying its source — the uploaded case", () => {
    const state = rehydrate(emptyWorkspace, [doc({ id: 1, source: "agent" })]);
    const announce: AnnouncedDoc = {
      ...doc({ id: 42, source: "uploaded" }),
      content: "edited upload",
    };
    const next = upsertFromAnnounce(state, announce);
    const uploaded = next.docs.filter((d) => d.source === "uploaded");
    expect(uploaded.map((d) => d.id)).toEqual([42]);
    expect(next.docs[0].id).toBe(42); // newest activity first
  });

  it("updates an existing card in place and re-orders by activity", () => {
    const state = rehydrate(emptyWorkspace, [
      doc({ id: 1 }),
      doc({ id: 2, title: "old title" }),
    ]);
    const next = upsertFromAnnounce(state, {
      ...doc({ id: 2, title: "new title" }),
    });
    expect(next.docs.map((d) => d.id)).toEqual([2, 1]);
    expect(next.docs[0].title).toBe("new title");
    expect(next.docs).toHaveLength(2);
  });

  it("ignores a tombstoned id — a late write must not resurrect the card", () => {
    let state = rehydrate(emptyWorkspace, [doc({ id: 5 })]);
    state = markDeleted(state, 5);
    const next = upsertFromAnnounce(state, {
      ...doc({ id: 5 }),
      content: "zombie",
    });
    expect(next.docs).toHaveLength(0);
  });
});

describe("rehydrate", () => {
  it("keeps tombstones across a mid-session refresh and filters them out", () => {
    let state = rehydrate(emptyWorkspace, [doc({ id: 1 }), doc({ id: 2 })]);
    state = markDeleted(state, 1);
    state = rehydrate(state, [doc({ id: 1 }), doc({ id: 2 }), doc({ id: 3 })]);
    expect(state.docs.map((d) => d.id)).toEqual([2, 3]); // 1 stays dead
    expect(state.deletedIds).toEqual([1]);
    // a REAL reload starts from empty state, so server truth still rules
    expect(rehydrate(emptyWorkspace, [doc({ id: 1 })]).docs).toHaveLength(1);
  });
});

describe("updateDocMeta (the user's own save, FR-56)", () => {
  it("moves char_count and title and re-sorts by activity", () => {
    const state = rehydrate(emptyWorkspace, [
      doc({ id: 1 }),
      doc({ id: 2, title: "old", char_count: 4 }),
    ]);
    const next = updateDocMeta(state, 2, "renamed.md", 999);
    expect(next.docs[0]).toMatchObject({
      id: 2,
      title: "renamed.md",
      char_count: 999,
    });
    expect(next.docs.map((d) => d.id)).toEqual([2, 1]);
    expect(updateDocMeta(state, 777, "x", 1)).toBe(state); // unknown: no-op
  });
});

describe("sorting", () => {
  const docs = [
    doc({ id: 1, title: "beta", char_count: 30, format: "pdf", created_at: "2026-10-01T00:00:00Z" }),
    doc({ id: 2, title: "Alpha", char_count: 10, format: "markdown", created_at: "2026-10-03T00:00:00Z", updated_at: "2026-10-09T00:00:00Z" }),
    doc({ id: 3, title: "gamma", char_count: 20, format: "text", created_at: "2026-10-02T00:00:00Z" }),
  ];

  it("sorts by each column, with updated falling back to created", () => {
    expect(sortDocs(docs, "name", "asc").map((d) => d.title)).toEqual([
      "Alpha",
      "beta",
      "gamma",
    ]);
    expect(sortDocs(docs, "size", "desc").map((d) => d.id)).toEqual([1, 3, 2]);
    expect(sortDocs(docs, "created", "desc").map((d) => d.id)).toEqual([2, 3, 1]);
    expect(sortDocs(docs, "updated", "desc").map((d) => d.id)).toEqual([2, 3, 1]);
    expect(sortDocs(docs, "type", "asc").map((d) => d.format)).toEqual([
      "markdown",
      "pdf",
      "text",
    ]);
  });

  it("nextSort is never idempotent: repeat clicks reverse", () => {
    const s1 = nextSort({ col: "updated", dir: "desc" }, "name");
    expect(s1).toEqual({ col: "name", dir: "asc" });
    const s2 = nextSort(s1, "name");
    expect(s2).toEqual({ col: "name", dir: "desc" });
    const s3 = nextSort(s2, "name");
    expect(s3).toEqual({ col: "name", dir: "asc" });
    expect(nextSort(s3, "size")).toEqual({ col: "size", dir: "desc" });
  });
});

describe("downloadFilename", () => {
  it("maps formats and never double-appends an extension", () => {
    expect(downloadFilename("notes", "markdown", 1)).toBe("notes.md");
    expect(downloadFilename("plan.md", "markdown", 1)).toBe("plan.md");
    expect(downloadFilename("notes", "text", 1)).toBe("notes.txt");
    expect(downloadFilename("scan.pdf", "pdf", 1)).toBe("scan.pdf.txt");
  });

  it("sanitizes the untrusted title: separators, control chars, empty", () => {
    expect(downloadFilename("../..\\evil:name", "text", 7)).toBe("....evilname.txt");
    expect(downloadFilename("a\u0000b\u001fc", "text", 7)).toBe("abc.txt");
    expect(downloadFilename("///", "text", 7)).toBe("document-7.txt");
    expect(downloadFilename("x".repeat(500), "text", 7).length).toBeLessThan(130);
  });
});
