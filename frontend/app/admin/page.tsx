"use client";

/**
 * /admin (FR-29): account list + disable/enable. The client-side gate here
 * is cosmetic UX — every API call below is enforced server-side by the
 * admin custom claim (FR-28).
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { authedFetch, useAuth } from "@/lib/auth";

type Account = {
  uid: string;
  email: string | null;
  display_name: string | null;
  email_verified: boolean;
  disabled: boolean;
  providers: string[];
  created_at: number | null;
  last_sign_in: number | null;
};

function when(ts: number | null): string {
  return ts ? new Date(ts).toLocaleString() : "—";
}

export default function AdminPage() {
  const router = useRouter();
  const { user, loading, isAdmin } = useAuth();
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [error, setError] = useState("");
  const [busyUid, setBusyUid] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError("");
    const res = await authedFetch("/api/admin/users");
    if (!res.ok) {
      setError(`couldn't load users (${res.status})`);
      return;
    }
    const body = (await res.json()) as { users: Account[] };
    setAccounts(body.users);
  }, []);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!isAdmin) {
      router.replace("/");
      return;
    }
    load();
  }, [loading, user, isAdmin, router, load]);

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

  if (loading || !user || !isAdmin) return null;

  return (
    <main className="stage admin-stage">
      <nav className="topnav" aria-label="account">
        <Link className="login-link" href="/">
          ← back
        </Link>
      </nav>
      <header className="masthead">
        <h1 className="wordmark">Admin</h1>
        <p className="tagline">accounts</p>
      </header>

      {error && <p className="error">{error}</p>}

      <div className="admin-table-wrap">
        <table className="admin-table">
          <thead>
            <tr>
              <th>email</th>
              <th>name</th>
              <th>providers</th>
              <th>verified</th>
              <th>created</th>
              <th>last sign-in</th>
              <th>status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {accounts.map((a) => (
              <tr key={a.uid} className={a.disabled ? "disabled-row" : ""}>
                <td>{a.email ?? a.uid}</td>
                <td>{a.display_name ?? "—"}</td>
                <td>{a.providers.join(", ")}</td>
                <td>{a.email_verified ? "yes" : "no"}</td>
                <td>{when(a.created_at)}</td>
                <td>{when(a.last_sign_in)}</td>
                <td>{a.disabled ? "disabled" : "active"}</td>
                <td>
                  <button
                    type="button"
                    className="login-btn small"
                    disabled={busyUid === a.uid}
                    onClick={() => setDisabled(a.uid, !a.disabled)}
                  >
                    {a.disabled ? "enable" : "disable"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}
