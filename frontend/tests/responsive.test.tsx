/**
 * FR-57.4's mandated responsive case, structurally: with the editor open
 * (full-width on phones via CSS), the console — End control included —
 * remains RENDERED inside the workspace, pinned outside the editor
 * overlay. jsdom can't evaluate media queries, so this pins the DOM
 * contract the phone CSS depends on: console and editor coexist.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Workspace } from "@/components/Workspace";
import { emptyWorkspace, rehydrate } from "@/lib/workspace";
import type { EditorTab } from "@/lib/useEditor";

afterEach(cleanup);

vi.mock("@uiw/react-codemirror", () => ({
  default: () => <div data-testid="cm" />,
}));
vi.mock("@codemirror/lang-markdown", () => ({ markdown: () => [] }));

const tab: EditorTab = {
  id: 1,
  title: "d1.md",
  format: "markdown",
  base: "x",
  baseToken: "t",
  buffer: "x",
  dirty: false,
  saveState: "saved",
  terminal: null,
  conflict: false,
  theirs: null,
  loading: false,
  loadFailed: false,
  view: "source",
};

describe("Workspace console docking (FR-57.4)", () => {
  it("renders the console (End control) alongside an open editor tab", () => {
    const noop = () => {};
    render(
      <Workspace
        state={rehydrate(emptyWorkspace, [
          {
            id: 1,
            title: "d1.md",
            kind: null,
            format: "markdown",
            source: "uploaded",
            char_count: 1,
          },
        ])}
        idle={false}
        attachedIds={[]}
        attachedChars={0}
        total={1}
        loading={false}
        error={null}
        budgetError={null}
        console={<button type="button">End</button>}
        editor={{
          tabs: [tab],
          activeId: 1,
          onActivate: noop,
          onClose: noop,
          onEdit: noop,
          onSetView: noop,
          onApplyTheirs: noop,
          onDismissConflict: noop,
        }}
        onClose={noop}
        onLoadMore={noop}
        onOpen={noop}
        onDownload={noop}
        onDelete={noop}
        onToggleAttach={noop}
        onUpload={async () => {}}
        onUploadsSettled={async () => {}}
      />,
    );
    // the editor is open…
    expect(screen.getByRole("tab", { name: /d1\.md/ })).toBeTruthy();
    expect(document.querySelector(".ed-pane")).toBeTruthy();
    // …and the console with its End control is still in the DOM, inside
    // the workspace (the phone overlay pins it via .has-tabs CSS)
    const ws = document.querySelector(".ws-v2.has-tabs");
    expect(ws).toBeTruthy();
    expect(screen.getByRole("button", { name: "End" })).toBeTruthy();
    expect(ws!.querySelector(".ws-console")).toBeTruthy();
  });
});
