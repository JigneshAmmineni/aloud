"use client";

/**
 * /admin/users — the user list (FR-35/FR-41): debounced search-as-you-type,
 * sortable column headers (click to sort, click again to reverse),
 * pagination below, FR-29's disable/enable kept per row. Clicking anywhere
 * else on a row opens that user's session history.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { authedFetch } from "@/lib/auth";
import {
  AdminShell,
  fmtCost,
  fmtInt,
  fmtMinutes,
  fmtWhen,
  type CostEstimate,
  type Usage,
} from "../shell";

type AdminUser = {
  uid: string;
  email: string | null;
  display_name: string | null;
  disabled: boolean;
  providers: string[];
  sessions: number;
  last_active: string | null;
  usage: Usage;
  estimated_cost: CostEstimate;
};

type UsersResponse = {
  users: AdminUser[];
  total: number;
  page: number;
  page_size: number;
};

const PAGE_SIZE = 25;

// column key -> server sort key (null = not sortable server-side)
const SORTABLE: Record<string, string> = {
  email: "email",
  sessions: "sessions",
  cost: "cost",
  last_active: "last_active",
};

export default function AdminUsersPage() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [sort, setSort] = useState("last_active");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<UsersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyUid, setBusyUid] = useState<string | null>(null);
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // FR-41: filters as you type, debounced. New search resets to page 1.
  const onSearch = (value: string) => {
    setQ(value);
    if (debounceTimer.current) clearTimeout(debounceTimer.current);
    debounceTimer.current = setTimeout(() => {
      setPage(1);
      setDebouncedQ(value);
    }, 300);
  };

  const load = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      const params = new URLSearchParams({
        q: debouncedQ,
        sort,
        order,
        page: String(page),
        page_size: String(PAGE_SIZE),
      });
      const res = await authedFetch(`/api/admin/users?${params}`);
      if (!res.ok) throw new Error(String(res.status));
      setData((await res.json()) as UsersResponse);
    } catch {
      setError("couldn't load users — try a reload");
    } finally {
      setLoading(false);
    }
  }, [debouncedQ, sort, order, page]);

  useEffect(() => {
    load();
  }, [load]);

  const onSort = (key: string) => {
    const serverKey = SORTABLE[key];
    if (!serverKey) return;
    if (sort === serverKey) {
      setOrder(order === "desc" ? "asc" : "desc");
    } else {
      setSort(serverKey);
      setOrder("desc");
    }
    setPage(1);
  };

  const arrow = (key: string) =>
    sort === SORTABLE[key] ? (order === "desc" ? " ↓" : " ↑") : "";

  const setDisabled = async (uid: string, disabled: boolean) => {
    setBusyUid(uid);
    setError("");
    try {
      const res = await authedFetch(
        `/api/admin/users/${uid}/${disabled ? "disable" : "enable"}`,
        { method: "POST" },
      );
      if (!res.ok) throw new Error(String(res.status));
      await load();
    } catch {
      setError("that didn't stick — try again");
    } finally {
      setBusyUid(null);
    }
  };

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  return (
    <AdminShell>
      <div className="admin-toolbar">
        <input
          className="login-field admin-search"
          type="search"
          placeholder="search email, name, or uid…"
          value={q}
          onChange={(e) => onSearch(e.target.value)}
          aria-label="search users"
        />
      </div>

      {error && <p className="error">{error}</p>}
      {loading && !data && <p className="login-msg notice">loading…</p>}

      {data && (
        <>
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th className="sortable" onClick={() => onSort("email")}>
                    email{arrow("email")}
                  </th>
                  <th>name</th>
                  <th>status</th>
                  <th className="sortable" onClick={() => onSort("sessions")}>
                    sessions{arrow("sessions")}
                  </th>
                  <th>audio min</th>
                  <th>tokens in</th>
                  <th>tokens out</th>
                  <th>tts chars</th>
                  <th className="sortable" onClick={() => onSort("cost")}>
                    est. cost{arrow("cost")}
                  </th>
                  <th className="sortable" onClick={() => onSort("last_active")}>
                    last active{arrow("last_active")}
                  </th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {data.users.length === 0 && (
                  <tr>
                    <td colSpan={11} className="admin-empty">
                      {debouncedQ ? "no users match that search" : "no users yet"}
                    </td>
                  </tr>
                )}
                {data.users.map((u) => (
                  <tr
                    key={u.uid}
                    className={`clickable${u.disabled ? " disabled-row" : ""}`}
                    onClick={() => router.push(`/admin/users/${u.uid}`)}
                  >
                    <td>{u.email ?? u.uid}</td>
                    <td>{u.display_name ?? "—"}</td>
                    <td>{u.disabled ? "disabled" : "active"}</td>
                    <td>{u.sessions}</td>
                    <td>{fmtMinutes(u.usage)}</td>
                    <td>{fmtInt(u.usage["llm.tokens_in"] ?? 0)}</td>
                    <td>{fmtInt(u.usage["llm.tokens_out"] ?? 0)}</td>
                    <td>{fmtInt(u.usage["tts.characters"] ?? 0)}</td>
                    <td>{fmtCost(u.estimated_cost)}</td>
                    <td>{fmtWhen(u.last_active)}</td>
                    <td>
                      <button
                        type="button"
                        className="login-btn small"
                        disabled={busyUid === u.uid}
                        onClick={(e) => {
                          e.stopPropagation();
                          setDisabled(u.uid, !u.disabled);
                        }}
                      >
                        {u.disabled ? "enable" : "disable"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="admin-pagination">
            <button
              type="button"
              className="login-btn small"
              disabled={page <= 1 || loading}
              onClick={() => setPage(page - 1)}
            >
              ← prev
            </button>
            <span className="admin-page-label">
              page {data.page} of {totalPages} · {data.total} user
              {data.total === 1 ? "" : "s"}
            </span>
            <button
              type="button"
              className="login-btn small"
              disabled={page >= totalPages || loading}
              onClick={() => setPage(page + 1)}
            >
              next →
            </button>
          </div>
        </>
      )}
    </AdminShell>
  );
}
