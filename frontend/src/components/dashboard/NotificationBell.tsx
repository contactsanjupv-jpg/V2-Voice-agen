"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useNotifications } from "@/lib/useNotifications";
import { NotificationPreferences } from "./NotificationPreferences";
import { CATEGORY_LABEL, badgeText, emptyMessage, safeInternalLink, timeLabel, type NotificationCategory, type NotificationItem } from "@/lib/notifications";

/**
 * The ONLY entry point to notifications: a bell in the Dashboard Overview header that opens a right-hand panel over the page.
 * It is rendered by app/dashboard/page.tsx and nowhere else (tests/notifications.test.ts enforces that).
 */

const ICON: Record<NotificationCategory, string> = {
  calls: "M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2Z",
  leads: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8a7 7 0 0 1 14 0",
  credits: "M12 6v6l4 2M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z",
  billing: "M3 7h18v10H3zM3 11h18",
  receptionist: "M12 3a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Zm-6 8a6 6 0 0 0 12 0m-6 6v3",
  phone: "M8 3h8a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm3 15h2",
  business: "M4 20V10l8-6 8 6v10M9 20v-6h6v6",
  analytics: "M4 20V10m6 10V4m6 16v-8m4 8H2",
  product: "m12 3 2.5 5.5L20 9l-4 4 1 6-5-3-5 3 1-6-4-4 5.5-.5Z",
};

function Icon({ category }: { category: NotificationCategory }) {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={ICON[category] ?? ICON.business} />
    </svg>
  );
}

export function NotificationBell({ orgId }: { orgId: string | undefined }) {
  const n = useNotifications(orgId);
  const { state } = n;
  const router = useRouter();
  const badge = badgeText(state.unread);
  const bellRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [mounted, setMounted] = useState(false);
  const [shown, setShown] = useState(false);
  const [view, setView] = useState<"list" | "preferences">("list");  // preferences live inside the drawer, never on a page of their own

  // Mount, then slide in; slide out, then unmount (state is only set from timer callbacks). The transition classes are motion-safe.
  useEffect(() => {
    if (state.open) {
      const t = setTimeout(() => {
        setMounted(true);
        requestAnimationFrame(() => setShown(true));
      }, 0);
      return () => clearTimeout(t);
    }
    const hide = setTimeout(() => setShown(false), 0);
    const unmount = setTimeout(() => setMounted(false), 220);
    return () => {
      clearTimeout(hide);
      clearTimeout(unmount);
    };
  }, [state.open]);

  const { close: closeDrawer } = n;
  useEffect(() => {
    if (!state.open || !mounted) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && closeDrawer();  // closing resets the view when the drawer unmounts
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [state.open, mounted, closeDrawer]);

  const close = () => {
    setView("list");
    n.close();
    bellRef.current?.focus();
  };

  const go = (item: NotificationItem) => {
    void n.markRead(item);
    const href = safeInternalLink(item.link);
    if (href) {
      n.close();
      router.push(href);
    }
  };

  return (
    <>
      <button
        ref={bellRef}
        type="button"
        onClick={n.open}
        aria-label={badge ? `Notifications, ${state.unread} unread` : "Notifications"}
        aria-haspopup="dialog"
        aria-expanded={state.open}
        className="relative inline-flex h-9 w-9 items-center justify-center rounded-full border border-[var(--color-line)] text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]"
      >
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M6 9a6 6 0 1 1 12 0c0 5 2 6 2 6H4s2-1 2-6Zm4 9a2 2 0 0 0 4 0" />
        </svg>
        {badge && (
          <span className="absolute -right-1 -top-1 min-w-[18px] rounded-full bg-[var(--color-ring)] px-1 text-center text-[11px] font-semibold leading-[18px] text-white" data-testid="notification-badge">
            {badge}
          </span>
        )}
      </button>

      {mounted && (
        <div className="fixed inset-0 z-50" role="presentation">
          <div
            className={`absolute inset-0 bg-black/30 motion-safe:transition-opacity motion-safe:duration-200 ${shown ? "opacity-100" : "opacity-0"}`}
            onClick={close}
            aria-hidden="true"
          />
          <aside
            role="dialog"
            aria-modal="true"
            aria-label="Notifications"
            className={`absolute right-0 top-0 flex h-full w-full flex-col border-l border-[var(--color-line)] bg-[var(--color-paper-raised)] shadow-xl motion-safe:transition-transform motion-safe:duration-200 sm:max-w-[420px] ${shown ? "translate-x-0" : "translate-x-full"}`}
          >
            <header className="flex items-center justify-between gap-3 border-b border-[var(--color-line)] px-5 py-4">
              <div className="min-w-0">
                <h2 className="font-[family-name:var(--font-display)] text-[17px] font-bold tracking-tight">{view === "list" ? "Notifications" : "Notification preferences"}</h2>
                <p className="text-[12.5px] text-[var(--color-ink-soft)]" aria-live="polite">
                  {view === "preferences" ? "Only affects you" : state.connection === "reconnecting" ? "Reconnecting…" : state.unread > 0 ? `${state.unread} unread` : "All caught up"}
                </p>
              </div>
              <button type="button" onClick={() => setView(view === "list" ? "preferences" : "list")} aria-label={view === "list" ? "Notification preferences" : "Back to notifications"}
                className="ml-auto rounded-full p-2 text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                {view === "list" ? (
                  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h2M10 17h10" /><circle cx="16" cy="7" r="2" /><circle cx="8" cy="17" r="2" /></svg>
                ) : (
                  <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M15 6l-6 6 6 6" /></svg>
                )}
              </button>
              <button ref={closeRef} type="button" onClick={close} aria-label="Close notifications" className="rounded-full p-2 text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg>
              </button>
            </header>

            {view === "preferences" ? <NotificationPreferences orgId={orgId} active={view === "preferences"} /> : (<>
            <div className="flex items-center justify-between gap-2 border-b border-[var(--color-line)] px-5 py-2.5">
              <div role="tablist" aria-label="Filter" className="inline-flex rounded-full border border-[var(--color-line)] p-0.5">
                {(["all", "unread"] as const).map((f) => (
                  <button key={f} role="tab" aria-selected={state.filter === f} onClick={() => n.setFilter(f)}
                    className={`rounded-full px-3 py-1 text-[12.5px] font-medium ${state.filter === f ? "bg-[var(--color-ink)] text-[var(--color-paper)]" : "text-[var(--color-ink-soft)]"}`}>
                    {f === "all" ? "All" : "Unread"}
                  </button>
                ))}
              </div>
              <button type="button" onClick={() => void n.markAllRead()} disabled={state.unread === 0}
                className="text-[12.5px] font-medium text-[var(--color-ink-soft)] underline-offset-2 hover:underline disabled:opacity-40 disabled:no-underline">
                Mark all as read
              </button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto" data-testid="notification-list">
              {(state.load === "loading" || state.load === "idle") && <p className="px-5 py-8 text-center text-[14px] text-[var(--color-ink-soft)]" role="status">Loading…</p>}
              {state.load === "error" && (
                <div className="px-5 py-8 text-center" role="alert">
                  <p className="text-[14px] text-[var(--color-ink-soft)]">We couldn&apos;t load your notifications.</p>
                  <button type="button" onClick={() => void n.retry()} className="mt-3 rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium">Try again</button>
                </div>
              )}
              {state.load === "ready" && state.items.length === 0 && <p className="px-5 py-10 text-center text-[14px] text-[var(--color-ink-soft)]">{emptyMessage(state.filter)}</p>}
              <ul>
                {state.items.map((item) => (
                  <li key={item.id} className="border-b border-[var(--color-line)] last:border-b-0">
                    <button type="button" onClick={() => go(item)} className="flex w-full gap-3 px-5 py-3.5 text-left hover:bg-[var(--color-paper)]" aria-label={`${item.is_read ? "" : "Unread: "}${item.title}`}>
                      <span className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-[var(--color-line)] ${item.severity === "critical" ? "text-red-600" : item.severity === "warning" ? "text-amber-700" : "text-[var(--color-ink-soft)]"}`}>
                        <Icon category={item.category} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-start justify-between gap-2">
                          <span className={`text-[14.5px] leading-snug ${item.is_read ? "font-medium text-[var(--color-ink-soft)]" : "font-semibold"}`}>{item.title}</span>
                          {!item.is_read && <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-[var(--color-ring)]" aria-hidden="true" />}
                        </span>
                        {item.body && <span className="mt-0.5 block text-[13.5px] leading-snug text-[var(--color-ink-soft)]">{item.body}</span>}
                        <span className="mt-1 block text-[12px] text-[var(--color-ink-soft)]/80">{CATEGORY_LABEL[item.category] ?? item.category} · {timeLabel(item.created_at)}</span>
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
              {state.nextCursor && (
                <div className="px-5 py-4 text-center">
                  <button type="button" onClick={() => void n.loadMore()} disabled={state.loadingMore} className="rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium disabled:opacity-50">
                    {state.loadingMore ? "Loading…" : "Load older"}
                  </button>
                </div>
              )}
            </div>
            </>)}
          </aside>
        </div>
      )}
    </>
  );
}
