/**
 * FR-54's mandated sanitization tests: document content is model- or
 * user-authored and untrusted, and the preview pane runs on the origin
 * holding the Firebase session. The renderer config IS the security
 * control, so these pin it: raw HTML never renders, a javascript: link
 * renders inert, and an image renders WITHOUT a network fetch.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { MarkdownView, safeHref } from "@/components/MarkdownView";

afterEach(cleanup);

describe("MarkdownView sanitization", () => {
  it("renders markdown structure", () => {
    const { container } = render(
      <MarkdownView content={"# Title\n\nhello **world**"} />,
    );
    expect(container.querySelector("h1")?.textContent).toBe("Title");
    expect(container.querySelector("strong")?.textContent).toBe("world");
  });

  it("renders a [x](javascript:...) link inert — no javascript: href anywhere", () => {
    const { container } = render(
      <MarkdownView content={"[x](javascript:alert(1))"} />,
    );
    expect(container.textContent).toContain("x");
    for (const a of Array.from(container.querySelectorAll("a"))) {
      expect(a.getAttribute("href") ?? "").not.toContain("javascript");
    }
    expect(container.innerHTML).not.toContain("javascript:");
  });

  it("neutralizes data: URLs the same way", () => {
    const { container } = render(
      <MarkdownView content={"[x](data:text/html,<script>1</script>)"} />,
    );
    expect(container.innerHTML).not.toContain("data:text/html");
  });

  it("keeps http/https/mailto links, hardened", () => {
    const { container } = render(
      <MarkdownView content={"[ok](https://example.com) [m](mailto:a@b.c)"} />,
    );
    const links = Array.from(container.querySelectorAll("a"));
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "https://example.com",
      "mailto:a@b.c",
    ]);
    for (const a of links) {
      expect(a.getAttribute("rel")).toContain("noopener");
    }
  });

  it("renders an ![x](https://...) image WITHOUT a network fetch — no img element", () => {
    // images auto-fetch on render: an external source beacons on preview,
    // an exfiltration channel under prompt injection (roadmap item 9)
    const { container } = render(
      <MarkdownView content={"![diagram](https://evil.example/beacon.png)"} />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("diagram"); // alt text shows
  });

  it("never renders raw HTML", () => {
    const { container } = render(
      <MarkdownView
        content={'before <img src="https://evil.example/b.png"> <script>1</script> after'}
      />,
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("before");
    expect(container.textContent).toContain("after");
  });
});

describe("safeHref", () => {
  it("allowlists schemes, not substrings", () => {
    expect(safeHref("https://a.b")).toBe("https://a.b");
    expect(safeHref("http://a.b")).toBe("http://a.b");
    expect(safeHref("mailto:a@b.c")).toBe("mailto:a@b.c");
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,x")).toBeNull();
    expect(safeHref("JAVASCRIPT:alert(1)")).toBeNull(); // case games
    expect(safeHref(" javascript:alert(1)")).toBeNull(); // whitespace games
    expect(safeHref("vbscript:x")).toBeNull();
  });
});
