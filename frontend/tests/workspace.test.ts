/**
 * FR-55's mandated client tests: the upsert/tombstone/refetch rules are a
 * pure reducer over announce payloads — the cheapest tests in the feature,
 * and each rule's failure is invisible in manual use.
 */
import { describe, expect, it } from "vitest";

import {
  downloadFilename,
  emptyWorkspace,
  markDeleted,
  rehydrate,
  setPreview,
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
  it("inserts an unknown id into the source-matched list — the uploaded case", () => {
    // FR-53's format gate made uploads editable; an edited upload must
    // land under Uploaded, never Created.
    const state = rehydrate(emptyWorkspace, [doc({ id: 1, source: "agent" })]);
    const announce: AnnouncedDoc = {
      ...doc({ id: 42, source: "uploaded" }),
      content: "edited upload",
    };
    const { state: next } = upsertFromAnnounce(state, announce);
    const uploaded = next.docs.filter((d) => d.source === "uploaded");
    expect(uploaded.map((d) => d.id)).toEqual([42]);
    expect(next.docs[0].id).toBe(42); // newest activity first
  });

  it("updates an existing card in place and re-orders by activity", () => {
    const state = rehydrate(emptyWorkspace, [
      doc({ id: 1 }),
      doc({ id: 2, title: "old title" }),
    ]);
    const { state: next } = upsertFromAnnounce(state, {
      ...doc({ id: 2, title: "new title" }),
    });
    expect(next.docs.map((d) => d.id)).toEqual([2, 1]);
    expect(next.docs[0].title).toBe("new title");
    expect(next.docs).toHaveLength(2);
  });

  it("re-renders the previewed document in place when content rides along", () => {
    let state = rehydrate(emptyWorkspace, [doc({ id: 7 })]);
    state = setPreview(state, 7, "before");
    const { state: next, refetch } = upsertFromAnnounce(state, {
      ...doc({ id: 7 }),
      content: "after",
      content_omitted: false,
    });
    expect(next.previewContent).toBe("after");
    expect(refetch).toBe(false); // no refetch while the payload carries it
  });

  it("asks for a refetch ONLY when content was omitted AND that doc is previewed", () => {
    let state = rehydrate(emptyWorkspace, [doc({ id: 7 }), doc({ id: 8 })]);
    state = setPreview(state, 7, "before");

    // omitted + previewed -> refetch
    const previewed = upsertFromAnnounce(state, {
      ...doc({ id: 7 }),
      content: null,
      content_omitted: true,
    });
    expect(previewed.refetch).toBe(true);
    expect(previewed.state.previewContent).toBe("before"); // kept until fetch

    // omitted + NOT previewed -> no refetch (the list updates from metadata)
    const background = upsertFromAnnounce(state, {
      ...doc({ id: 8 }),
      content: null,
      content_omitted: true,
    });
    expect(background.refetch).toBe(false);
  });

  it("ignores a tombstoned id — a late write must not resurrect the card", () => {
    // FR-46 lets a write outlive its turn, so a document.updated can land
    // after the user deleted that document: the client-side twin of the
    // resurrection FR-50 forbids at the DB.
    let state = rehydrate(emptyWorkspace, [doc({ id: 5 })]);
    state = markDeleted(state, 5);
    const { state: next, refetch } = upsertFromAnnounce(state, {
      ...doc({ id: 5 }),
      content: "zombie",
    });
    expect(next.docs).toHaveLength(0);
    expect(refetch).toBe(false);
  });
});

describe("markDeleted", () => {
  it("closes the pane when the previewed document is deleted", () => {
    let state = rehydrate(emptyWorkspace, [doc({ id: 5 }), doc({ id: 6 })]);
    state = setPreview(state, 5, "open");
    state = markDeleted(state, 5);
    expect(state.previewId).toBeNull();
    expect(state.previewContent).toBeNull();
    // deleting a non-previewed doc leaves the pane alone
    state = setPreview(state, 6, "open");
    state = markDeleted(state, 99);
    expect(state.previewId).toBe(6);
  });
});

describe("rehydrate", () => {
  it("replaces the list with server truth and clears tombstones — a reload shows server truth", () => {
    let state = rehydrate(emptyWorkspace, [doc({ id: 1 }), doc({ id: 2 })]);
    state = markDeleted(state, 1);
    state = rehydrate(state, [doc({ id: 2 }), doc({ id: 3 })]);
    expect(state.docs.map((d) => d.id)).toEqual([2, 3]);
    expect(state.deletedIds).toEqual([]);
  });

  it("keeps the preview only while its document still exists", () => {
    let state = rehydrate(emptyWorkspace, [doc({ id: 1 })]);
    state = setPreview(state, 1, "open");
    state = rehydrate(state, [doc({ id: 1 })]);
    expect(state.previewId).toBe(1);
    state = rehydrate(state, [doc({ id: 9 })]);
    expect(state.previewId).toBeNull();
  });
});

describe("downloadFilename", () => {
  it("maps formats and never double-appends an extension", () => {
    expect(downloadFilename("notes", "markdown", 1)).toBe("notes.md");
    expect(downloadFilename("plan.md", "markdown", 1)).toBe("plan.md"); // not plan.md.md
    expect(downloadFilename("notes", "text", 1)).toBe("notes.txt");
    // pdf stores extracted text: .txt, never a broken .pdf
    expect(downloadFilename("scan.pdf", "pdf", 1)).toBe("scan.pdf.txt");
  });

  it("sanitizes the untrusted title: separators, control chars, empty", () => {
    expect(downloadFilename("../..\\evil:name", "text", 7)).toBe("....evilname.txt");
    expect(downloadFilename("a\u0000b\u001fc", "text", 7)).toBe("abc.txt");
    expect(downloadFilename("///", "text", 7)).toBe("document-7.txt");
    expect(downloadFilename("x".repeat(500), "text", 7).length).toBeLessThan(130);
  });
});
