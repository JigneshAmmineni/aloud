"use client";

import { useRef, useState } from "react";
import type { AttachedDocument } from "@/lib/useAloudSession";

function formatChars(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k chars` : `${n} chars`;
}

/**
 * Pre-session affordance to attach documents the agent will read and discuss.
 * Shown only while idle (the parent decides). Uploads go to /documents; the
 * returned ids ride into the session at start time.
 */
export function DocumentUpload({
  documents,
  onUpload,
  onRemove,
}: {
  documents: AttachedDocument[];
  onUpload: (file: File) => Promise<void>;
  onRemove: (id: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setError(null);
    setBusy(true);
    try {
      for (const file of Array.from(files)) {
        await onUpload(file);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "couldn't read that file");
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = ""; // allow re-selecting
    }
  };

  return (
    <section className="doc-upload" aria-label="Documents">
      {documents.length > 0 && (
        <ul className="doc-list">
          {documents.map((d) => (
            <li key={d.id} className="doc-item">
              <span className="doc-name">{d.filename}</span>
              <span className="doc-meta">{formatChars(d.char_count)}</span>
              <button
                type="button"
                className="doc-remove"
                aria-label={`Remove ${d.filename}`}
                onClick={() => onRemove(d.id)}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      <button
        type="button"
        className="doc-attach"
        disabled={busy}
        onClick={() => inputRef.current?.click()}
      >
        {busy ? "reading…" : documents.length ? "+ attach another" : "+ attach a document"}
      </button>
      <input
        ref={inputRef}
        type="file"
        accept=".txt,.md,.markdown,.pdf,text/plain,text/markdown,application/pdf"
        multiple
        hidden
        onChange={(e) => handleFiles(e.target.files)}
      />

      {error && <p className="doc-error">{error}</p>}
    </section>
  );
}
