"use client";

import ReactMarkdown from "react-markdown";

/**
 * The sanitized markdown renderer (FR-54). Document content is model- or
 * user-authored and untrusted in the render context — the preview pane runs
 * on the origin holding the Firebase session, so:
 *  - raw HTML never renders (skipHtml; no rehype-raw, ever),
 *  - link URLs are scheme-allowlisted to http/https/mailto — javascript:
 *    and data: URLs survive every HTML-disabled renderer,
 *  - images do not render at all: they are a separate node type the link
 *    allowlist does not touch, and they FETCH automatically on render — an
 *    external image source beacons on preview (an exfiltration channel
 *    under prompt injection). The alt text shows instead.
 */

const ALLOWED_SCHEMES = ["http:", "https:", "mailto:"];

export function safeHref(url: string | undefined): string | null {
  if (!url) return null;
  try {
    // Relative URLs resolve against the app origin — harmless, allowed.
    const parsed = new URL(url, "https://relative.invalid");
    return ALLOWED_SCHEMES.includes(parsed.protocol) ? url : null;
  } catch {
    return null;
  }
}

export function MarkdownView({ content }: { content: string }) {
  return (
    <div className="md-view">
      <ReactMarkdown
        skipHtml
        urlTransform={(url) => safeHref(url) ?? ""}
        components={{
          a: ({ href, children }) => {
            const safe = safeHref(href);
            // a disallowed scheme renders as plain text — inert, no href
            if (!safe) return <span>{children}</span>;
            return (
              <a href={safe} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
          // images never render: alt text only, no fetch
          img: ({ alt }) => <span className="md-img-alt">[{alt || "image"}]</span>,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
