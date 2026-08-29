"use client";

/**
 * Shared chrome for all admin pages (FR-41): the auth gate, the persistent
 * Overview | Users tab bar, and the breadcrumb trail for drill-down paths.
 * The client-side gate is cosmetic UX — every admin API call is enforced
 * server-side by the admin custom claim (FR-28), scoped by FR-38.
 */

import { useEffect, type ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { useAuth } from "@/lib/auth";

export type Crumb = { label: string; href?: string };

/** True once Firebase auth has initialized AND the user is an admin. Pages
 * must gate their data fetches on this: on a hard reload the component
 * mounts before auth is ready, and an ungated fetch goes out tokenless →
 * 401 → a sticky error state. */
export function useAdminReady(): boolean {
  const { user, loading, isAdmin } = useAuth();
  return !loading && !!user && isAdmin;
}

export type CostEstimate = { stt: number; llm: number; tts: number; total: number };

/** usage maps "stage.unit" -> quantity (the backend aggregate shape). */
export type Usage = Record<string, number>;

export function fmtWhen(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const s = Math.round(seconds);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

export function fmtMinutes(usage: Usage): string {
  const secs = usage["stt.seconds"] ?? 0;
  return (secs / 60).toFixed(1);
}

export function fmtInt(n: number | null | undefined): string {
  return n == null ? "—" : Math.round(n).toLocaleString();
}

/** FR-34: every cost figure is labeled an estimate. */
export function fmtCost(c: CostEstimate | undefined): string {
  return c ? `$${c.total.toFixed(4)}` : "—";
}

export function AdminShell({
  crumbs,
  children,
}: {
  crumbs?: Crumb[];
  children: ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, loading, isAdmin } = useAuth();

  useEffect(() => {
    if (loading) return;
    if (!user) {
      router.replace("/login");
      return;
    }
    if (!isAdmin) router.replace("/");
  }, [loading, user, isAdmin, router]);

  if (loading || !user || !isAdmin) return null;

  // "← back" follows the breadcrumb: the last crumb with an href; top-level
  // admin pages fall back to leaving admin for the app.
  const backHref =
    crumbs
      ?.slice()
      .reverse()
      .find((c) => c.href)?.href ?? "/";

  return (
    <main className="stage admin-stage">
      <nav className="topnav" aria-label="account">
        <Link className="login-link" href={backHref}>
          ← back
        </Link>
      </nav>
      <header className="masthead">
        <h1 className="wordmark">Admin</h1>
        <nav className="admin-tabs" aria-label="admin sections">
          <Link
            href="/admin"
            className={`admin-tab${pathname === "/admin" ? " active" : ""}`}
          >
            Overview
          </Link>
          <Link
            href="/admin/users"
            className={`admin-tab${pathname?.startsWith("/admin/users") ? " active" : ""}`}
          >
            Users
          </Link>
        </nav>
        {crumbs && crumbs.length > 0 && (
          <nav className="admin-crumbs" aria-label="breadcrumb">
            {crumbs.map((c, i) => (
              <span key={i} className="admin-crumb">
                {i > 0 && <span className="admin-crumb-sep">→</span>}
                {c.href ? <Link href={c.href}>{c.label}</Link> : c.label}
              </span>
            ))}
          </nav>
        )}
      </header>
      {children}
    </main>
  );
}
