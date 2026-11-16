/**
 * Deleting from the document list (FR-52): a repeat tap while the first
 * delete is still in flight sends nothing, and a delete that succeeds
 * clears an earlier "couldn't delete" message.
 */
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useWorkspace } from "@/lib/useWorkspace";
import type { WorkspaceDoc } from "@/lib/workspace";

const { authedFetch } = vi.hoisted(() => ({ authedFetch: vi.fn() }));
vi.mock("@/lib/auth", () => ({ authedFetch }));

const doc = (id: number): WorkspaceDoc => ({
  id,
  title: `d${id}.md`,
  kind: null,
  format: "markdown",
  source: "uploaded",
  char_count: 10,
});

const response = (status: number): Response =>
  ({ ok: status >= 200 && status < 300, status }) as unknown as Response;

beforeEach(() => {
  authedFetch.mockReset();
});

describe("useWorkspace deleteDoc", () => {
  it("a repeat tap while the delete is in flight sends no second request and raises no error", async () => {
    let finish: (r: Response) => void = () => {};
    authedFetch.mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
    );
    // what the server says to a second DELETE of the same document
    authedFetch.mockResolvedValue(response(404));
    const { result } = renderHook(() => useWorkspace(false));

    let first: Promise<boolean> = Promise.resolve(false);
    let second = true;
    await act(async () => {
      first = result.current.deleteDoc(doc(1));
      second = await result.current.deleteDoc(doc(1));
      finish(response(200));
      await first;
    });

    expect(authedFetch).toHaveBeenCalledTimes(1);
    expect(second).toBe(false);
    expect(await first).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it("a delete that succeeds clears an earlier failure's message", async () => {
    authedFetch.mockResolvedValueOnce(response(500));
    authedFetch.mockResolvedValueOnce(response(200));
    const { result } = renderHook(() => useWorkspace(false));

    await act(async () => {
      await result.current.deleteDoc(doc(1));
    });
    expect(result.current.error).toBe("couldn't delete that document");

    await act(async () => {
      await result.current.deleteDoc(doc(1));
    });
    expect(result.current.error).toBeNull();
  });
});
