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
  started_at: string;
  ended_at: string | null;
  status: string;
  end_reason: string | null;
  duration_s: number | null;
  usage: Usage;
  estimated_cost: CostEstimate;
  turns: Turn[];
};

/** compact "ttfb.llm 800 · ttfb.tts 120" line from the stages_ms JSON */
function fmtStages(stages: Record<string, number> | null): string {
  if (!stages || Object.keys(stages).length === 0) return "—";
  return Object.entries(stages)
    .map(([k, v]) => `${k.replace(/^ttfb\./, "")} ${v}ms`)
    .join(" · ");
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

  // Breadcrumb: Users → {uid} → session. Deliberately the uid, not the
  // email — these deep links are meant to be shared, and an email in a URL
  // lands in browser history and access logs. The email is one click up.
  const who = data?.user_id ?? "user";

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
            counts); ⚠ = over the {NFR1_BUDGET_MS / 1000}s NFR-1 budget
          </p>
        </>
      )}
    </AdminShell>
  );
}
