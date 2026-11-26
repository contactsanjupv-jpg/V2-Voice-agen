/**
 * In-app notification centre logic (pure, no React): the state machine the drawer and the bell are built on, plus the helpers
 * that keep it safe (deep links) and readable (categories, times). Kept pure so it is tested without a browser.
 */
import { relativeTime } from "./dashboard";

export type NotificationCategory = "calls" | "leads" | "credits" | "billing" | "receptionist" | "phone" | "business" | "analytics" | "product";
export type NotificationSeverity = "info" | "warning" | "critical";

export type NotificationItem = {
  id: string;
  seq: number;
  kind: string;
  category: NotificationCategory;
  severity: NotificationSeverity;
  title: string;
  body: string;
  link: string | null;
  created_at: string;
  read_at: string | null;
  is_read: boolean;
};

export type NotificationPage = { items: NotificationItem[]; next_cursor: number | null; unread_count: number };
export type NotificationStateOut = { latest_seq: number; unread_count: number };

export const CATEGORY_LABEL: Record<NotificationCategory, string> = {
  calls: "Calls",
  leads: "Leads",
  credits: "Credits",
  billing: "Billing",
  receptionist: "Receptionist",
  phone: "Phone",
  business: "Business",
  analytics: "Analytics",
  product: "Product updates",
};

/** Accent for the category dot/icon: the app's own tokens only. */
export const SEVERITY_TONE: Record<NotificationSeverity, "neutral" | "warn" | "bad"> = { info: "neutral", warning: "warn", critical: "bad" };

/** The badge text: nothing at zero, "99+" above 99. */
export function badgeText(unread: number): string | null {
  if (!Number.isFinite(unread) || unread <= 0) return null;
  return unread > 99 ? "99+" : String(Math.floor(unread));
}

/** A deep link is only ever a relative dashboard path. Anything else (another site, a script URL, "//host") is refused. */
export function safeInternalLink(link: string | null | undefined): string | null {
  if (!link) return null;
  return /^\/dashboard(\/[A-Za-z0-9_-]+)*([?][A-Za-z0-9_=&.-]*)?(#[A-Za-z0-9_-]*)?$/.test(link) ? link : null;
}

export function timeLabel(iso: string, now = Date.now()): string {
  return relativeTime(iso, now) ?? "";
}

export type Connection = "connecting" | "live" | "reconnecting" | "offline";
export type Filter = "all" | "unread";
export type Load = "idle" | "loading" | "ready" | "error";

export type NotificationsState = {
  /** The organization this state belongs to. Results tagged with any other organization are dropped. */
  orgId: string | null;
  open: boolean;
  filter: Filter;
  load: Load;
  loadingMore: boolean;
  items: NotificationItem[];
  nextCursor: number | null;
  unread: number;
  latestSeq: number;
  /** The server reported something newer than what the open list shows: fetch the first page again. */
  stale: boolean;
  connection: Connection;
};

export function initialStateFor(orgId: string | null): NotificationsState {
  return { ...initialState, orgId };
}

export const initialState: NotificationsState = {
  orgId: null, open: false, filter: "all", load: "idle", loadingMore: false, items: [], nextCursor: null, unread: 0, latestSeq: 0, stale: false, connection: "connecting",
};

/** Every result that arrives from the network carries the organization it was requested for (`org`). */
export type Action =
  | { type: "switch-org"; orgId: string | null }
  | { type: "open" }
  | { type: "close" }
  | { type: "filter"; filter: Filter }
  | { type: "load-start" }
  | { type: "loaded"; org: string; page: NotificationPage }
  | { type: "load-failed"; org: string }
  | { type: "more-start" }
  | { type: "more-loaded"; org: string; page: NotificationPage }
  | { type: "more-failed"; org: string }
  | { type: "marked-read"; org: string; id: string; unread: number }
  | { type: "marked-all"; org: string; unread: number }
  | { type: "server-state"; org: string; state: NotificationStateOut }
  | { type: "connection"; org: string; connection: Connection };

const maxSeq = (items: NotificationItem[]) => items.reduce((m, i) => Math.max(m, i.seq), 0);

/** Items already shown keep their position; a page never repeats an id. */
export function mergePage(existing: NotificationItem[], incoming: NotificationItem[]): NotificationItem[] {
  const seen = new Set(existing.map((i) => i.id));
  return [...existing, ...incoming.filter((i) => !seen.has(i.id))].sort((a, b) => b.seq - a.seq);
}

export function reducer(s: NotificationsState, a: Action): NotificationsState {
  if (a.type === "switch-org") return a.orgId === s.orgId ? s : initialStateFor(a.orgId);  // nothing of the previous organization survives
  if ("org" in a && a.org !== s.orgId) return s;  // a late response, event or poll from another organization: ignored
  switch (a.type) {
    case "open":
      return { ...s, open: true, stale: false };  // opening NEVER marks anything read
    case "close":
      return { ...s, open: false };  // history is kept in state when the drawer closes
    case "filter":
      return a.filter === s.filter ? s : { ...s, filter: a.filter, load: "idle", items: [], nextCursor: null };  // the open drawer reloads for the new filter
    case "load-start":
      return { ...s, load: s.items.length ? s.load : "loading", stale: false };
    case "loaded":
      return { ...s, load: "ready", items: a.page.items, nextCursor: a.page.next_cursor, unread: a.page.unread_count, latestSeq: Math.max(s.latestSeq, maxSeq(a.page.items)), stale: false };
    case "load-failed":
      return { ...s, load: s.items.length ? s.load : "error" };
    case "more-start":
      return { ...s, loadingMore: true };
    case "more-loaded":
      return { ...s, loadingMore: false, items: mergePage(s.items, a.page.items), nextCursor: a.page.next_cursor, unread: a.page.unread_count };
    case "more-failed":
      return { ...s, loadingMore: false };
    case "marked-read":
      return {
        ...s, unread: a.unread,
        items: s.items.map((i) => (i.id === a.id && !i.is_read ? { ...i, is_read: true, read_at: i.read_at ?? new Date().toISOString() } : i)),
      };
    case "marked-all":
      return { ...s, unread: a.unread, items: s.items.map((i) => (i.is_read ? i : { ...i, is_read: true, read_at: i.read_at ?? new Date().toISOString() })) };
    case "server-state": {
      const newer = a.state.latest_seq > s.latestSeq;
      return { ...s, unread: a.state.unread_count, latestSeq: Math.max(s.latestSeq, a.state.latest_seq), stale: s.stale || (newer && s.load !== "idle") };
    }
    case "connection":
      return s.connection === a.connection ? s : { ...s, connection: a.connection };
  }
}

/** Parses one Server-Sent Event payload; anything unexpected is ignored (never throws into the UI). */
export function parseStateEvent(data: string): NotificationStateOut | null {
  try {
    const v = JSON.parse(data) as Partial<NotificationStateOut>;
    return typeof v.latest_seq === "number" && typeof v.unread_count === "number" && v.unread_count >= 0 ? { latest_seq: v.latest_seq, unread_count: v.unread_count } : null;
  } catch {
    return null;
  }
}

/** Reconnect delays for the polling fallback (ms): 5 s, then doubling to a 60 s ceiling. */
export function backoffMs(attempt: number): number {
  return Math.min(5_000 * 2 ** Math.max(0, attempt), 60_000);
}

export const POLL_MS = 30_000;

export function emptyMessage(filter: Filter): string {
  return filter === "unread" ? "You're all caught up." : "Nothing here yet. Alerts about your leads, minutes and receptionist will show up here.";
}
