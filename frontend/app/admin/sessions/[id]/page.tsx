"use client";

/**
 * /admin/sessions/{id} — the drill-down (FR-36/FR-41): session summary
 * (duration, end reason, usage, estimated cost) and the per-turn table
 * joining latency with that turn's usage and cost, turns over the 3s NFR-1
 * budget visually flagged. A turn may have cost but no latency (barge-in
 * before first audio) — the union shape from the backend. Usage only,
 * never content (NFR-9).
 */

import { useCallback, useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { authedFetch } from "@/lib/auth";
import {
  AdminShell,
  fmtCost,
  fmtDuration,
  fmtInt,
  fmtWhen,
  useAdminReady,
  type CostEstimate,
  type Usage,
} from "../../shell";

// Mirrors E2E_ERROR_S in backend/obs/latency.py — the one budget definition;
// change them together.
const NFR1_BUDGET_MS = 3000;

type Turn = {
  turn_id: number;
  eot_to_first_audio_ms: number | null;
  stages_ms: Record<string, number> | null;
  usage: Usage;
  estimated_cost: CostEstimate;
};

type Detail = {
  session_id: string;
  user_id: string;
  user_email: string | null;
  started_at: string;
  ended_at: string | null;
  status: string;
  end_reason: string | null;
  duration_s: number | null;
  usage: Usage;
  estimated_cost: CostEstimate;
  turns: Turn[];
};

/** Vocabulary-aware stage line (FR-47): rows written before §4.10 carry
 * flat keys (ttfb.llm, tool.x) and render as before; the agent loop's rows
 * carry step-indexed keys (step.N.ttfb.llm, step.N.tool.x, step.N.filler)
 * and render grouped per step — with an expected-but-absent step TTFB
 * shown as "—", never omitted-therefore-fine. */
function fmtStages(stages: Record<string, number> | null): string {
  if (!stages || Object.keys(stages).length === 0) return "—";
  const entries = Object.entries(stages);
  if (!entries.some(([k]) => k.startsWith("step."))) {
    // legacy vocabulary — history must not retroactively render as broken
    return entries
      .map(([k, v]) => `${k.replace(/^ttfb\./, "")} ${v}ms`)
      .join(" · ");
  }
  const steps = new Map<number, { ttfb?: number; parts: string[] }>();
  for (const [key, v] of entries) {
    const m = key.match(/^step\.(\d+)\.(.+)$/);
    if (!m) continue;
    const n = Number(m[1]);
    const rest = m[2];
    if (!steps.has(n)) steps.set(n, { parts: [] });
    const step = steps.get(n)!;
    if (rest === "ttfb.llm") step.ttfb = v;
    else if (rest === "filler") step.parts.push(`filler@${v}ms`);
    else step.parts.push(`${rest.replace(/^tool\./, "")} ${v}ms`);
  }
  return [...steps.entries()]
    .sort(([a], [b]) => a - b)
    .map(([n, s]) =>
      [`s${n} llm ${s.ttfb != null ? `${s.ttfb}ms` : "—"}`, ...s.parts].join(" "),
    )
    .join(" · ");
}

/** FR-36 amendment: a filler-led turn spoke a canned line while tool work
 * ran — its healthy-looking first-audio number must say so. */
function isFillerLed(stages: Record<string, number> | null): boolean {
  return !!stages && Object.keys(stages).some((k) => /^step\.\d+\.filler$/.test(k));
}

export default function AdminSessionPage() {
  const ready = useAdminReady();
  const { id } = useParams<{ id: string }>();
  const [data, setData] = useState<Detail | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const res = await authedFetch(`/api/admin/sessions/${id}`);
      if (res.status === 404) {
        setError("no such session");
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      setData((await res.json()) as Detail);
    } catch {
      setError("couldn't load this session — try a reload");
    }
  }, [id]);

  useEffect(() => {
    if (ready) load();
  }, [ready, load]);

  // Breadcrumb: Users → {email} → session (FR-41). The email is resolved
  // SERVER-side and never travels in the URL — these deep links are meant
  // to be shared, and a URL param would land in history and access logs.
  const who = data?.user_email ?? data?.user_id ?? "user";

  return (
    <AdminShell
      crumbs={[
        { label: "Users", href: "/admin/users" },
        ...(data
          ? [{ label: who, href: `/admin/users/${data.user_id}` }]
          : [{ label: who }]),
        { label: "session" },
      ]}
    >
      {error && <p className="error">{error}</p>}
      {!data && !error && <p className="login-msg notice">loading…</p>}

      {data && (
        <>
          <div className="admin-summary">
            <h2 className="admin-card-title">session {data.session_id}</h2>
            <p className="admin-card-detail">
              {fmtWhen(data.started_at)} · {fmtDuration(data.duration_s)} ·
              ended: {data.end_reason ?? data.status}
            </p>
            <p className="admin-card-detail">
              {fmtInt(data.usage["llm.tokens_in"] ?? 0)} tokens in ·{" "}
              {fmtInt(data.usage["llm.tokens_out"] ?? 0)} out ·{" "}
              {fmtInt(data.usage["tts.characters"] ?? 0)} tts chars · est.{" "}
              {fmtCost(data.estimated_cost)}
            </p>
          </div>

          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>turn</th>
                  <th>speech → first audio</th>
                  <th>tokens in</th>
                  <th>tokens out</th>
                  <th>tts chars</th>
                  <th>est. cost</th>
                  <th>stage breakdown</th>
                </tr>
              </thead>
              <tbody>
                {data.turns.length === 0 && (
                  <tr>
                    <td colSpan={7} className="admin-empty">
                      no per-turn data for this session
                    </td>
                  </tr>
                )}
                {data.turns.map((t) => {
                  const ms = t.eot_to_first_audio_ms;
                  const breach = ms != null && ms > NFR1_BUDGET_MS;
                  return (
                    <tr key={t.turn_id} className={breach ? "error-row" : ""}>
                      <td>{t.turn_id}</td>
                      <td className={breach ? "breach" : ""}>
                        {ms != null ? `${ms}ms${breach ? " ⚠" : ""}` : "—"}
                        {isFillerLed(t.stages_ms) ? " 🗨" : ""}
                      </td>
                      <td>{fmtInt(t.usage["llm.tokens_in"] ?? 0)}</td>
                      <td>{fmtInt(t.usage["llm.tokens_out"] ?? 0)}</td>
                      <td>{fmtInt(t.usage["tts.characters"] ?? 0)}</td>
                      <td>{fmtCost(t.estimated_cost)}</td>
                      <td className="admin-stages">{fmtStages(t.stages_ms)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="admin-footnote">
            “—” latency = turn interrupted before first audio (its spend still
            counts); ⚠ = over the {NFR1_BUDGET_MS / 1000}s NFR-1 budget; 🗨 =
            filler-led — first audio was a canned line while tool work ran, not
            a fast answer
          </p>
        </>
      )}
    </AdminShell>
  );
}
