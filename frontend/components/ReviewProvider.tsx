"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useState } from "react";

import { Toast, type ToastState } from "@/components/Toast";
import { api, ApiError } from "@/lib/api";
import type { QueueCounts, Store, User } from "@/lib/types";
import { useLiveEvents, type LiveStatus } from "@/lib/useLiveEvents";

interface ReviewContext {
  stores: Store[];
  categories: string[];
  storeId: number | undefined;
  setStoreId: (id: number | undefined) => void;
  counts: QueueCounts;
  refreshCounts: () => void;
  /** Adjust a badge locally after a decision, so it doesn't lag a round trip. */
  bumpCount: (queue: keyof QueueCounts, delta: number) => void;
  notify: (message: string, tone?: "info" | "error") => void;
  /** Bumps whenever the backend says something changed — pass into a `useQueue` deps
   *  array to have that queue quietly refetch instead of waiting for the next reload. */
  liveTick: number;
  liveStatus: LiveStatus;
}

const Ctx = createContext<ReviewContext | null>(null);

export function useReview(): ReviewContext {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useReview must be used inside the review layout");
  return ctx;
}

const TABS = [
  { href: "/review/category", label: "Category", key: "category" as const },
  { href: "/review/dimensions", label: "Dimensions", key: "dimensions" as const },
  { href: "/review/icons", label: "2D Icons", key: "icon_2d" as const },
  { href: "/review/import", label: "Import", key: null },
];

export function ReviewProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();

  const [user, setUser] = useState<User | null>(null);
  const [stores, setStores] = useState<Store[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [storeId, setStoreId] = useState<number | undefined>(undefined);
  const [counts, setCounts] = useState<QueueCounts>({ category: 0, dimensions: 0, icon_2d: 0 });
  const [toast, setToast] = useState<ToastState | null>(null);

  const notify = useCallback((message: string, tone: "info" | "error" = "info") => {
    setToast({ message, tone });
  }, []);

  // Gate on the session before anything else renders.
  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch(() => router.replace("/login"));
  }, [router]);

  const live = useLiveEvents(Boolean(user), () => router.replace("/login"));

  useEffect(() => {
    if (!user) return;
    api
      .vocabulary()
      .then((v) => {
        setStores(v.stores);
        setCategories(v.categories);
      })
      .catch(() => notify("Could not load reference data.", "error"));
  }, [user, notify]);

  const refreshCounts = useCallback(() => {
    api.counts(storeId).then(setCounts).catch(() => undefined);
  }, [storeId]);

  useEffect(() => {
    if (user) refreshCounts();
  }, [user, refreshCounts, live.tick]);

  const bumpCount = useCallback((queue: keyof QueueCounts, delta: number) => {
    setCounts((c) => ({ ...c, [queue]: Math.max(0, c[queue] + delta) }));
  }, []);

  async function signOut() {
    try {
      await api.logout();
    } catch (err) {
      if (!(err instanceof ApiError)) return;
    }
    router.replace("/login");
  }

  if (!user) return <div className="centre" style={{ color: "var(--muted)" }}>Loading…</div>;

  return (
    <Ctx.Provider
      value={{
        stores,
        categories,
        storeId,
        setStoreId,
        counts,
        refreshCounts,
        bumpCount,
        notify,
        liveTick: live.tick,
        liveStatus: live.status,
      }}
    >
      <header className="header">
        <div className="header-bar">
          <span className="brand">Zory pipeline</span>
          <nav className="tabs">
            {TABS.map((tab) => (
              <Link
                key={tab.href}
                href={tab.href}
                className="tab"
                data-active={pathname === tab.href}
              >
                {tab.label}
                {tab.key && <span className="badge">{counts[tab.key]}</span>}
              </Link>
            ))}
          </nav>
          <span className="spacer" />
          <span
            className="live-dot"
            data-status={live.status}
            title={
              live.status === "live"
                ? "Live — updates as reviewers and stages finish work"
                : live.status === "reconnecting"
                  ? "Reconnecting — updates may be delayed until this comes back"
                  : "Connecting…"
            }
          />
          <select
            value={storeId ?? ""}
            onChange={(e) => setStoreId(e.target.value ? Number(e.target.value) : undefined)}
            aria-label="Filter by store"
          >
            <option value="">All stores</option>
            {stores.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name_english}
              </option>
            ))}
          </select>
          <button className="ghost" onClick={signOut}>
            {user.username} · sign out
          </button>
        </div>
      </header>

      <main className="main">{children}</main>
      <Toast toast={toast} />
    </Ctx.Provider>
  );
}
