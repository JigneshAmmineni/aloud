"use client";

/**
 * /admin — the Overview tab (FR-37/FR-41), the default admin landing view:
 * live sessions, sessions & unique users today/7d, estimated spend by
 * provider (7d/30d), turn latency p50/p95 + NFR-1 breaches over 24h, and
 * link-outs to Cloud Logging / Error Reporting for error inspection (FR-39).
 */

import { useCallback, useEffect, useState } from "react";

import { authedFetch } from "@/lib/auth";
import {
  AdminShell,
  fmtInt,
  useAdminReady,
  type CostEstimate,
  type Usage,
} from "./shell";

const GCP_PROJECT = "aloud-498522";
const LOGS_URL = `https://console.cloud.google.com/logs/query?project=${GCP_PROJECT}`;
const ERRORS_URL = `https://console.cloud.google.com/errors?project=${GCP_PROJECT}`;

type Overview = {
  live_sessions: number;
  today: { sessions: number; unique_users: number };
  last_7d: { sessions: number; unique_users: number };
  usage_7d: Usage;
  usage_30d: Usage;
  estimated_cost_7d: CostEstimate;
  estimated_cost_30d: CostEstimate;
  turn_latency_24h: {
    p50_ms: number | null;
    p95_ms: number | null;
    turns: number;
    nfr1_breaches: number;
  };
  error_sessions_24h: number;
};

function Spend({ label, cost }: { label: string; cost: CostEstimate }) {
  return (
    <div className="admin-card">
      <h2 className="admin-card-title">est. spend — {label}</h2>
      <p className="admin-stat">${cost.total.toFixed(2)}</p>
      <p className="admin-card-detail">
        stt ${cost.stt.toFixed(2)} · llm ${cost.llm.toFixed(2)} · tts $
        {cost.tts.toFixed(2)}
      </p>
    </div>
  );
}

export default function AdminOverviewPage() {
  const ready = useAdminReady();
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const res = await authedFetch("/api/admin/overview");
      if (!res.ok) throw new Error(String(res.status));
      setData((await res.json()) as Overview);
    } catch {
      setError("couldn't load the overview — try a reload");
    }
  }, []);

  useEffect(() => {
    if (ready) load();
  }, [ready, load]);

  const lat = data?.turn_latency_24h;

  return (
    <AdminShell>
      {error && <p className="error">{error}</p>}
      {!data && !error && <p className="login-msg notice">loading…</p>}
      {data && (
        <div className="admin-cards">
          <div className="admin-card">
            <h2 className="admin-card-title">live sessions</h2>
            <p className="admin-stat">{data.live_sessions}</p>
            <p className="admin-card-detail">in-process count; resets on deploy</p>
          </div>
          <div className="admin-card">
            <h2 className="admin-card-title">today</h2>
            <p className="admin-stat">{data.today.sessions}</p>
            <p className="admin-card-detail">
              sessions · {data.today.unique_users} unique user
              {data.today.unique_users === 1 ? "" : "s"}
            </p>
          </div>
          <div className="admin-card">
            <h2 className="admin-card-title">last 7 days</h2>
            <p className="admin-stat">{data.last_7d.sessions}</p>
            <p className="admin-card-detail">
              sessions · {data.last_7d.unique_users} unique user
              {data.last_7d.unique_users === 1 ? "" : "s"}
            </p>
          </div>
          <Spend label="7d" cost={data.estimated_cost_7d} />
          <Spend label="30d" cost={data.estimated_cost_30d} />
          <div className="admin-card">
            <h2 className="admin-card-title">turn latency — 24h</h2>
            <p className="admin-stat">
              {lat?.p50_ms != null ? `${lat.p50_ms}ms` : "—"}
            </p>
            <p className="admin-card-detail">
              p50 · p95 {lat?.p95_ms != null ? `${lat.p95_ms}ms` : "—"} ·{" "}
              {fmtInt(lat?.turns)} turns
            </p>
          </div>
          <div className="admin-card">
            <h2 className="admin-card-title">budget breaches — 24h</h2>
            <p
              className={`admin-stat${(lat?.nfr1_breaches ?? 0) > 0 ? " breach" : ""}`}
            >
              {lat?.nfr1_breaches ?? 0}
            </p>
            <p className="admin-card-detail">turns over the 3s NFR-1 budget</p>
          </div>
          <div className="admin-card">
            <h2 className="admin-card-title">error sessions — 24h</h2>
            <p
              className={`admin-stat${data.error_sessions_24h > 0 ? " breach" : ""}`}
            >
              {data.error_sessions_24h}
            </p>
            <p className="admin-card-detail">
              sessions ended with reason “error”
            </p>
          </div>
          <div className="admin-card">
            <h2 className="admin-card-title">error inspection</h2>
            <p className="admin-card-detail admin-card-links">
              <a href={LOGS_URL} target="_blank" rel="noreferrer">
                Logs Explorer ↗
              </a>
              <a href={ERRORS_URL} target="_blank" rel="noreferrer">
                Error Reporting ↗
              </a>
            </p>
            <p className="admin-card-detail">
              filter logs by session_id / event / severity
            </p>
          </div>
        </div>
      )}
      <p className="admin-footnote">
        all dollar figures are estimates at current configured rates; provider
        consoles are the invoice truth (FR-34)
      </p>
    </AdminShell>
  );
}
