/**
 * FR-57.3's remaining mandated component cases: the preview toggle
 * renders through the SAME sanitized MarkdownView (no second render
 * path), and pdf opens with no editable surface.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EditorPane } from "@/components/EditorPane";
import type { EditorTab } from "@/lib/useEditor";

afterEach(cleanup);

vi.mock("@uiw/react-codemirror", () => ({
  default: () => <div data-testid="cm" />,
}));
vi.mock("@codemirror/lang-markdown", () => ({ markdown: () => [] }));
vi.mock("@codemirror/state", () => ({
  Annotation: { define: () => ({ of: () => ({}) }) },
}));

const base: EditorTab = {
  id: 1,
  title: "d1.md",
  baseTitle: "d1.md",
  format: "markdown",
  base: "x",
  baseToken: "t",
  buffer: "x",
  dirty: false,
  saveState: "saved",
  terminal: null,
  terminalKind: null,
  stalled: false,
  conflict: false,
  loading: false,
  loadFailed: false,
  view: "preview",
};

const noop = () => {};
const handlers = {
  onActivate: noop,
  onClose: noop,
  onEdit: noop,
  onSetView: noop,
  onApplyTheirs: noop,
  onDismissConflict: noop,
  onRename: noop,
  onRetryStalled: noop,
  onDiscard: noop,
};

describe("EditorPane render paths", () => {
  it("markdown preview renders through the sanitized MarkdownView", () => {
    const tab = {
      ...base,
      buffer: "# Title\n\n[x](javascript:alert(1)) **bold**",
    };
    const { container } = render(
      <EditorPane tabs={[tab]} activeId={1} {...handlers} />,
    );
    // the real MarkdownView: structure renders, the scheme stays inert
    expect(container.querySelector(".md-view h1")?.textContent).toBe("Title");
    expect(container.querySelector("strong")?.textContent).toBe("bold");
    expect(container.innerHTML).not.toContain("javascript:");
    expect(screen.queryByTestId("cm")).toBeNull(); // preview, not source
  });

  it("pdf opens read-only: no editor surface, no view toggle", () => {
    const tab = {
      ...base,
      id: 2,
      format: "pdf",
      view: "source" as const,
      buffer: "extracted text",
    };
    const { container } = render(
      <EditorPane tabs={[tab]} activeId={2} {...handlers} />,
    );
    expect(screen.queryByTestId("cm")).toBeNull(); // never editable
    expect(container.querySelector(".ws-pre")?.textContent).toBe(
      "extracted text",
    );
    expect(container.querySelector(".ed-toggle")).toBeNull();
    expect(screen.getByText("read-only (extracted text)")).toBeTruthy();
  });
});
