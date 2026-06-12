"use client";

import type { Artifact } from "@/lib/useAloudSession";

const KIND_LABELS: Record<string, string> = {
  summary: "summary",
  action_items: "action items",
  cleaned_idea: "cleaned-up idea",
};

/** FR-12: appears only when an artifact exists (SDD §2.1). */
export function ArtifactsPanel({ artifacts }: { artifacts: Artifact[] }) {
  if (artifacts.length === 0) return null;

  return (
    <aside className="artifacts" aria-label="Artifacts">
      <h2 className="artifacts-heading">Written up</h2>
      {artifacts.map((a) => (
        <article key={a.id} className="artifact-card">
          <span className="artifact-kind">{KIND_LABELS[a.kind] ?? a.kind}</span>
          <h3 className="artifact-title">{a.title}</h3>
          <pre className="artifact-content">{a.content}</pre>
        </article>
      ))}
    </aside>
  );
}
