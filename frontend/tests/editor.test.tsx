/**
 * FR-57/58 editor-hook tests — the mandated cases that moved here from
 * the preview pane (§4.12 extends FR-55's rules to editor tabs): one tab
 * per document, the blank-buffer data-loss guard, clean-buffer announce
 * re-render (race case 2), dirty-buffer merge with the refetched
 * `theirs` on omitted content, the opaque CAS token chain at the client
 * boundary, and terminal-save handling.
 */
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useEditor } from "@/lib/useEditor";
import type { WorkspaceDoc } from "@/lib/workspace";

const { authedFetch } = vi.hoisted(() => ({ authedFetch: vi.fn() }));
vi.mock("@/lib/auth", () => ({ authedFetch }));

const doc = (id: number, over: Partial<WorkspaceDoc> = {}): WorkspaceDoc => ({
  id,
  title: `d${id}.md`,
  kind: null,
  format: "markdown",
  source: "uploaded",
  char_count: 10,
  ...over,
});

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

beforeEach(() => {
  authedFetch.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

const GET_BODY = {
  content: "line one\nline two\n",
  updated_at: "2026-10-10T12:00:00.123456+00:00",
  title: "d1.md",
  format: "markdown",
};

describe("useEditor", () => {
  it("one tab per document — re-open focuses, never duplicates", async () => {
    authedFetch.mockResolvedValue(jsonResponse(200, GET_BODY));
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    await act(async () => {
      result.current.activate(null as unknown as number);
      await result.current.open(doc(1));
    });
    expect(result.current.tabs).toHaveLength(1);
    expect(result.current.activeId).toBe(1);
  });

  it("a failed content fetch yields the error state with NO editable buffer", async () => {
    // FR-57.3's guard: a blank buffer autosaving over a document that
    // failed to load is the worst outcome in the feature
    authedFetch.mockRejectedValue(new Error("network"));
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    const tab = result.current.tabs[0];
    expect(tab.loadFailed).toBe(true);
    expect(tab.buffer).toBe("");
    // and a save never fires for it, even if marked dirty by accident
    expect(
      authedFetch.mock.calls.filter(([, init]) => init?.method === "PUT"),
    ).toHaveLength(0);
  });

  it("race case 2: a clean-buffer announce re-renders the open editor", async () => {
    authedFetch.mockResolvedValue(jsonResponse(200, GET_BODY));
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.handleAnnounce("document.updated", {
        ...doc(1),
        content: "agent rewrote this\n",
        content_omitted: false,
        updated_at: "TOK-NEW",
      } as never);
    });
    const tab = result.current.tabs[0];
    expect(tab.buffer).toBe("agent rewrote this\n");
    expect(tab.base).toBe("agent rewrote this\n");
    expect(tab.dirty).toBe(false);
  });

  it("dirty buffer + omitted announce: refetches theirs and MERGES (never blind-ignore)", async () => {
    authedFetch.mockResolvedValueOnce(jsonResponse(200, GET_BODY)); // open
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.edit(1, "line one MINE\nline two\n"); // dirty
    });
    // the announce omitted content; the refetch returns the agent's edit
    authedFetch.mockResolvedValueOnce(
      jsonResponse(200, {
        content: "line one\nline two AGENT\n",
        updated_at: "TOK-2",
      }),
    );
    await act(async () => {
      result.current.handleAnnounce("document.updated", {
        ...doc(1),
        content: null,
        content_omitted: true,
      } as never);
      await Promise.resolve();
    });
    await waitFor(() => {
      const tab = result.current.tabs[0];
      expect(tab.buffer).toBe("line one MINE\nline two AGENT\n");
    });
    const tab = result.current.tabs[0];
    expect(tab.conflict).toBe(false); // disjoint lines: both survive
    expect(tab.baseToken).toBe("TOK-2");
  });

  it("the CAS token round-trips byte-for-byte: GET string -> PUT base -> 200 string -> next PUT", async () => {
    authedFetch.mockResolvedValueOnce(jsonResponse(200, GET_BODY)); // open
    authedFetch.mockResolvedValueOnce(
      jsonResponse(200, { updated_at: "TOK-AFTER-SAVE-1", char_count: 5 }),
    );
    authedFetch.mockResolvedValueOnce(
      jsonResponse(200, { updated_at: "TOK-AFTER-SAVE-2", char_count: 6 }),
    );
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.edit(1, "edit1");
    });
    await act(async () => {
      await result.current.flushAll();
    });
    const put1 = authedFetch.mock.calls.find(([, i]) => i?.method === "PUT");
    expect(JSON.parse(put1![1].body).base_updated_at).toBe(
      GET_BODY.updated_at, // the exact string, never parsed/reformatted
    );
    act(() => {
      result.current.edit(1, "edit2!");
    });
    await act(async () => {
      await result.current.flushAll();
    });
    const puts = authedFetch.mock.calls.filter(([, i]) => i?.method === "PUT");
    expect(JSON.parse(puts[1][1].body).base_updated_at).toBe("TOK-AFTER-SAVE-1");
  });

  it("a 409 merges against the current row and flags overlap (user's lines kept)", async () => {
    authedFetch.mockResolvedValueOnce(jsonResponse(200, GET_BODY)); // open
    authedFetch.mockResolvedValueOnce(
      jsonResponse(409, {
        content: "line one THEIRS\nline two\n",
        updated_at: "TOK-409",
        title: "d1.md",
      }),
    );
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.edit(1, "line one MINE\nline two\n");
    });
    await act(async () => {
      await result.current.flushAll();
    });
    const tab = result.current.tabs[0];
    expect(tab.buffer).toContain("line one MINE"); // user precedence
    expect(tab.conflict).toBe(true);
    expect(tab.baseToken).toBe("TOK-409");
  });

  it("terminal 404 stops the retry loop, names it, keeps the buffer; flushAll blocks Talk", async () => {
    authedFetch.mockResolvedValueOnce(jsonResponse(200, GET_BODY)); // open
    authedFetch.mockResolvedValueOnce(
      jsonResponse(404, { detail: "document not found" }),
    );
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.edit(1, "only copy of my words");
    });
    let ok = true;
    await act(async () => {
      ok = await result.current.flushAll();
    });
    const tab = result.current.tabs[0];
    expect(tab.terminal).toContain("deleted elsewhere");
    expect(tab.buffer).toBe("only copy of my words"); // exportable
    expect(ok).toBe(false); // the pre-/start flush refuses Talk
    // no further PUTs after terminal, even on another edit
    const putsBefore = authedFetch.mock.calls.filter(
      ([, i]) => i?.method === "PUT",
    ).length;
    await act(async () => {
      result.current.edit(1, "more typing");
      await result.current.flushAll();
    });
    const putsAfter = authedFetch.mock.calls.filter(
      ([, i]) => i?.method === "PUT",
    ).length;
    expect(putsAfter).toBe(putsBefore);
  });
});

describe("useEditor round-2 fixes", () => {
  it("flushAll returns TRUE after a successful flush — Talk must not refuse a 200", async () => {
    authedFetch.mockResolvedValueOnce(jsonResponse(200, GET_BODY)); // open
    authedFetch.mockResolvedValueOnce(
      jsonResponse(200, { updated_at: "TOK-OK", char_count: 5 }),
    );
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.edit(1, "fresh words");
    });
    let ok = false;
    await act(async () => {
      ok = await result.current.flushAll();
    });
    expect(ok).toBe(true); // the round-1 root-cause bug, pinned
    expect(result.current.tabs[0].dirty).toBe(false);
    expect(result.current.tabs[0].saveState).toBe("saved");
  });

  it("the first close on a cleanly-flushed dirty tab closes it", async () => {
    authedFetch.mockResolvedValueOnce(jsonResponse(200, GET_BODY));
    authedFetch.mockResolvedValueOnce(
      jsonResponse(200, { updated_at: "TOK-OK", char_count: 5 }),
    );
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.edit(1, "fresh words");
    });
    await act(async () => {
      await result.current.close(1);
    });
    expect(result.current.tabs).toHaveLength(0);
  });

  it("applyTheirs flips ONLY the conflicted region — clean merges and later typing survive", async () => {
    authedFetch.mockResolvedValueOnce(
      jsonResponse(200, { ...GET_BODY, content: "alpha\nbravo\ncharlie\n" }),
    );
    // the save 409s with the agent having edited alpha AND charlie
    authedFetch.mockResolvedValueOnce(
      jsonResponse(409, {
        content: "alpha THEIRS\nbravo\ncharlie AGENT\n",
        updated_at: "TOK-409",
        title: "d1.md",
      }),
    );
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      // the user edited alpha (conflicts) — bravo/charlie untouched
      result.current.edit(1, "alpha MINE\nbravo\ncharlie\n");
    });
    await act(async () => {
      await result.current.flushAll();
    });
    let tab = result.current.tabs[0];
    expect(tab.conflict).toBe(true);
    expect(tab.buffer).toContain("alpha MINE"); // user precedence held
    expect(tab.buffer).toContain("charlie AGENT"); // clean hunk merged in

    act(() => {
      result.current.applyTheirs(1);
    });
    tab = result.current.tabs[0];
    expect(tab.buffer).toContain("alpha THEIRS"); // the flip
    expect(tab.buffer).toContain("charlie AGENT"); // clean merge KEPT
    expect(tab.conflict).toBe(false);
  });

  it("retryable failures back off instead of hammering at 1 Hz", async () => {
    vi.useFakeTimers();
    authedFetch.mockResolvedValueOnce(jsonResponse(200, GET_BODY)); // open
    authedFetch.mockResolvedValue(jsonResponse(500, {})); // every save 500s
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.edit(1, "v2");
    });
    const puts = () =>
      authedFetch.mock.calls.filter(([, i]) => i?.method === "PUT").length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000); // debounce -> save 1 fails
    });
    expect(puts()).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000); // flat 1 Hz would re-PUT here
    });
    expect(puts()).toBe(1); // backoff holds (first retry at ~2s + debounce)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(puts()).toBeGreaterThanOrEqual(2); // it does retry
    expect(puts()).toBeLessThan(8); // and never at 1 Hz (30s of hammering = 30)
  });
});

describe("useEditor round-2 blockers", () => {
  it("flushAll issues a PUT for a tab whose previous save FAILED — Talk recovers", async () => {
    authedFetch.mockResolvedValueOnce(jsonResponse(200, GET_BODY)); // open
    authedFetch.mockResolvedValueOnce(jsonResponse(500, {})); // save 1 fails
    authedFetch.mockResolvedValueOnce(
      jsonResponse(200, { updated_at: "TOK-RECOVERED", char_count: 2 }),
    );
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.edit(1, "v2");
    });
    await act(async () => {
      await result.current.flushAll(); // hits the 500
    });
    expect(result.current.tabs[0].dirty).toBe(true);
    let ok = false;
    await act(async () => {
      ok = await result.current.flushAll(); // must PUT again, not no-op
    });
    expect(ok).toBe(true);
    expect(result.current.tabs[0].saveState).toBe("saved");
  });

  it("401 is retryable: the editor never goes read-only over a token blip", async () => {
    authedFetch.mockResolvedValueOnce(jsonResponse(200, GET_BODY));
    authedFetch.mockResolvedValueOnce(jsonResponse(401, {}));
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.edit(1, "v2");
    });
    await act(async () => {
      await result.current.flushAll();
    });
    const tab = result.current.tabs[0];
    expect(tab.terminal).toBeNull(); // not terminal
    expect(tab.saveState).toBe("unsaved"); // retryable, buffer kept
  });

  it("discard reverts to the last synced content and unblocks flushAll", async () => {
    authedFetch.mockResolvedValueOnce(jsonResponse(200, GET_BODY));
    authedFetch.mockResolvedValue(jsonResponse(500, {})); // saves keep failing
    const { result } = renderHook(() => useEditor());
    await act(async () => {
      await result.current.open(doc(1));
    });
    act(() => {
      result.current.edit(1, "doomed edit");
    });
    await act(async () => {
      await result.current.flushAll();
    });
    expect(result.current.tabs[0].dirty).toBe(true);
    act(() => {
      result.current.discard(1);
    });
    const tab = result.current.tabs[0];
    expect(tab.buffer).toBe(GET_BODY.content); // back to synced truth
    expect(tab.dirty).toBe(false);
    let ok = false;
    await act(async () => {
      ok = await result.current.flushAll();
    });
    expect(ok).toBe(true); // Talk unblocked by the user's explicit choice
  });
});
