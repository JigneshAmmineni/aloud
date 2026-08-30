"use client";

/**
 * /admin/users/{uid} — one user's session history (FR-36/FR-41): account
 * summary at top (email, name, status, totals), sessions table below,
 * newest first. Clicking a session row opens the drill-down.
 */

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";

import { authedFetch } from "@/lib/auth";
import {
  AdminShell,
  fmtCost,
  fmtDuration,
  fmtInt,
  fmtMinutes,
  fmtWhen,
  useAdminReady,
  type CostEstimate,
  type Usage,
} from "../../shell";

type Account = {
  uid: string;
  email: string | null;
  display_name: string | null;
  disabled: boolean;
  providers: string[];
};

type SessionRow = {
  session_id: string;
  started_at: string;
  duration_s: number | null;
  status: string;
  end_reason: string | null;
  usage: Usage;
  artifact_count: number;
  median_turn_ms: number | null;
  worst_turn_ms: number | null;
  estimated_cost: CostEstimate;
};

type Response = {
  account: Account | null;
  sessions: SessionRow[];
  // TRUE session count — the list is capped server-side (newest 500), and
  // the cap must be visible, never silent under-reporting on a cost view
  total_sessions: number;
};

export default function AdminUserSessionsPage() {
  const router = useRouter();
  const ready = useAdminReady();
  const { uid } = useParams<{ uid: string }>();
  const [data, setData] = useState<Response | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const res = await authedFetch(`/api/admin/users/${uid}/sessions`);
      if (!res.ok) throw new Error(String(res.status));
      setData((await res.json()) as Response);
    } catch {
      setError("couldn't load this user — try a reload");
    }
  }, [uid]);

  useEffect(() => {
    if (ready) load();
  }, [ready, load]);

  const who = data?.account?.email ?? uid;
  const totalCost = data
    ? data.sessions.reduce((sum, s) => sum + s.estimated_cost.total, 0)
    : 0;

  return (
    <AdminShell
      crumbs={[{ label: "Users", href: "/admin/users" }, { label: who }]}
    >
      {error && <p className="error">{error}</p>}
      {!data && !error && <p className="login-msg notice">loading…</p>}

      {data && (
        <>
          <div className="admin-summary">
            <h2 className="admin-card-title">{who}</h2>
            <p className="admin-card-detail">
              {data.account
                ? `${data.account.display_name ?? "no name"} · ${
                    data.account.disabled ? "disabled" : "active"
                  } · ${data.account.providers.join(", ")}`
                : "no Firebase account found for this uid"}
            </p>
            <p className="admin-card-detail">
              {data.total_sessions > data.sessions.length
                ? `showing newest ${data.sessions.length} of ${data.total_sessions} sessions (totals below cover only these)`
                : `${data.total_sessions} session${data.total_sessions === 1 ? "" : "s"}`}{" "}
              · est. total{" "}
              {fmtCost({
                stt: 0,
                llm: 0,
                tts: 0,
                total: totalCost,
                configured: data.sessions.some(
                  (s) => s.estimated_cost.configured,
                ),
              })}
            </p>
          </div>

          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>started</th>
                  <th>duration</th>
                  <th>end reason</th>
                  <th>artifacts</th>
                  <th>audio min</th>
                  <th>tokens in</th>
                  <th>tokens out</th>
                  <th>tts chars</th>
                  <th>est. cost</th>
                  <th>median turn</th>
                  <th>worst turn</th>
                </tr>
              </thead>
              <tbody>
                {data.sessions.length === 0 && (
                  <tr>
                    <td colSpan={11} className="admin-empty">
                      no sessions yet
                    </td>
                  </tr>
                )}
                {data.sessions.map((s) => (
                  <tr
                    key={s.session_id}
                    className={`clickable${s.end_reason === "error" ? " error-row" : ""}`}
                    tabIndex={0}
                    onClick={() => router.push(`/admin/sessions/${s.session_id}`)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter")
                        router.push(`/admin/sessions/${s.session_id}`);
                    }}
                  >
                    <td>{fmtWhen(s.started_at)}</td>
                    <td>{fmtDuration(s.duration_s)}</td>
                    <td>{s.end_reason ?? s.status}</td>
                    <td>{s.artifact_count}</td>
                    <td>{fmtMinutes(s.usage)}</td>
                    <td>{fmtInt(s.usage["llm.tokens_in"] ?? 0)}</td>
                    <td>{fmtInt(s.usage["llm.tokens_out"] ?? 0)}</td>
                    <td>{fmtInt(s.usage["tts.characters"] ?? 0)}</td>
                    <td>{fmtCost(s.estimated_cost)}</td>
                    <td>{s.median_turn_ms != null ? `${s.median_turn_ms}ms` : "—"}</td>
                    <td>{s.worst_turn_ms != null ? `${s.worst_turn_ms}ms` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="admin-footnote">
            audio minutes are the session's connect→disconnect duration, a
            proxy for streamed STT time; costs are estimates at current rates
          </p>
        </>
      )}
    </AdminShell>
  );
}
