import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import {
  backoffMs, badgeText, emptyMessage, initialStateFor, mergePage, parseStateEvent, reducer, safeInternalLink, timeLabel,
  type Action, type NotificationItem, type NotificationsState,
} from "../src/lib/notifications.ts";

const item = (over: Partial<NotificationItem> = {}): NotificationItem => ({
  id: "n1", seq: 1, kind: "payment_failed", category: "billing", severity: "critical", title: "Payment failed", body: "Update your payment method.",
  link: "/dashboard/settings/billing", created_at: "2026-11-23T10:00:00Z", read_at: null, is_read: false, ...over,
});
const A = "org-a";
const B = "org-b";
const run = (actions: Action[], from: NotificationsState = initialStateFor(A)) => actions.reduce(reducer, from);
const page = (items: NotificationItem[], unread = items.filter((i) => !i.is_read).length, next: number | null = null) => ({ items, next_cursor: next, unread_count: unread });

// ---- drawer open / close
test("the drawer opens and closes, and closing keeps the history", () => {
  let s = run([{ type: "open" }]);
  assert.equal(s.open, true);
  s = run([{ type: "loaded", org: A, page: page([item({ id: "a", seq: 2 }), item({ id: "b", seq: 1 })]) }, { type: "close" }], s);
  assert.equal(s.open, false);
  assert.equal(s.items.length, 2);  // history survives the close
});

test("opening the drawer never marks anything read", () => {
  const s = run([{ type: "loaded", org: A, page: page([item({ id: "a", seq: 2 }), item({ id: "b", seq: 1 })]) }, { type: "open" }]);
  assert.equal(s.unread, 2);
  assert.ok(s.items.every((i) => !i.is_read));
});

// ---- unread badge
test("the badge hides at zero, shows the count, and caps at 99+", () => {
  assert.equal(badgeText(0), null);
  assert.equal(badgeText(-3), null);
  assert.equal(badgeText(NaN), null);
  assert.equal(badgeText(7), "7");
  assert.equal(badgeText(99), "99");
  assert.equal(badgeText(100), "99+");
});

test("the unread count follows the server: live events and read actions", () => {
  let s = run([{ type: "server-state", org: A, state: { latest_seq: 5, unread_count: 3 } }]);
  assert.equal(s.unread, 3);
  s = run([{ type: "loaded", org: A, page: page([item({ id: "a", seq: 5 })], 3) }, { type: "marked-read", org: A, id: "a", unread: 2 }], s);
  assert.equal(s.unread, 2);
  s = run([{ type: "marked-all", org: A, unread: 0 }], s);
  assert.equal(s.unread, 0);
});

// ---- notifications display, read / unread
test("items are shown newest first and an incoming page never repeats an id", () => {
  const merged = mergePage([item({ id: "b", seq: 2 }), item({ id: "a", seq: 1 })], [item({ id: "b", seq: 2 }), item({ id: "c", seq: 3 })]);
  assert.deepEqual(merged.map((i) => i.id), ["c", "b", "a"]);
});

test("marking one read changes only that item, and keeps its first read time", () => {
  const base = run([{ type: "loaded", org: A, page: page([item({ id: "a", seq: 2 }), item({ id: "b", seq: 1 })]) }]);
  const s = run([{ type: "marked-read", org: A, id: "a", unread: 1 }], base);
  assert.deepEqual(s.items.map((i) => [i.id, i.is_read]), [["a", true], ["b", false]]);
  const again = run([{ type: "marked-read", org: A, id: "a", unread: 1 }], s);
  assert.equal(again.items[0].read_at, s.items[0].read_at);
});

test("mark all as read marks every loaded item", () => {
  const s = run([{ type: "loaded", org: A, page: page([item({ id: "a", seq: 2 }), item({ id: "b", seq: 1 })]) }, { type: "marked-all", org: A, unread: 0 }]);
  assert.ok(s.items.every((i) => i.is_read));
  assert.equal(s.unread, 0);
});

test("loading older pages appends them and keeps the cursor", () => {
  let s = run([{ type: "loaded", org: A, page: page([item({ id: "b", seq: 2 })], 1, 2) }, { type: "more-start" }]);
  assert.equal(s.loadingMore, true);
  s = run([{ type: "more-loaded", org: A, page: page([item({ id: "a", seq: 1, is_read: true })], 1, null) }], s);
  assert.deepEqual(s.items.map((i) => i.id), ["b", "a"]);
  assert.equal(s.nextCursor, null);
  assert.equal(s.loadingMore, false);
});

// ---- empty / loading / error
test("loading, empty and error states", () => {
  let s = run([{ type: "load-start" }]);
  assert.equal(s.load, "loading");
  s = run([{ type: "loaded", org: A, page: page([]) }], s);
  assert.equal(s.load, "ready");
  assert.equal(s.items.length, 0);
  assert.match(emptyMessage("all"), /Nothing here yet/);
  assert.match(emptyMessage("unread"), /all caught up/);
  assert.equal(run([{ type: "load-start" }, { type: "load-failed", org: A }]).load, "error");
});

test("a failed refresh keeps what is already shown instead of replacing it with an error", () => {
  const s = run([{ type: "loaded", org: A, page: page([item()]) }, { type: "load-start" }, { type: "load-failed", org: A }]);
  assert.equal(s.load, "ready");
  assert.equal(s.items.length, 1);
});

test("switching the filter reloads for the new filter", () => {
  const s = run([{ type: "loaded", org: A, page: page([item()]) }, { type: "filter", filter: "unread" }]);
  assert.equal(s.filter, "unread");
  assert.equal(s.load, "idle");  // the open drawer sees "idle" and fetches
  assert.equal(s.items.length, 0);
});

// ---- organization switch (no leakage between organizations)
test("switching organization clears items, badge, cursor and filter at once", () => {
  const a = run([
    { type: "open" },
    { type: "loaded", org: A, page: { items: [item({ id: "a1", seq: 7 })], next_cursor: 7, unread_count: 3 } },
    { type: "filter", filter: "unread" },
    { type: "loaded", org: A, page: { items: [item({ id: "a1", seq: 7 })], next_cursor: 7, unread_count: 3 } },
  ]);
  assert.equal(a.items.length, 1);
  const b = reducer(a, { type: "switch-org", orgId: B });
  assert.deepEqual(b, initialStateFor(B));
  assert.equal(b.items.length, 0);
  assert.equal(b.unread, 0);
  assert.equal(b.nextCursor, null);
  assert.equal(b.latestSeq, 0);
  assert.equal(badgeText(b.unread), null);
});

test("late responses, stream events and polls from the previous organization cannot touch the new one", () => {
  const a = run([{ type: "loaded", org: A, page: { items: [item({ id: "a1", seq: 7 })], next_cursor: 7, unread_count: 3 } }]);
  const b = reducer(a, { type: "switch-org", orgId: B });
  const late: Action[] = [
    { type: "loaded", org: A, page: { items: [item({ id: "a2", seq: 9 })], next_cursor: null, unread_count: 5 } },
    { type: "more-loaded", org: A, page: { items: [item({ id: "a3", seq: 2 })], next_cursor: null, unread_count: 5 } },
    { type: "server-state", org: A, state: { latest_seq: 99, unread_count: 8 } },
    { type: "marked-read", org: A, id: "a1", unread: 2 },
    { type: "marked-all", org: A, unread: 0 },
    { type: "connection", org: A, connection: "live" },
    { type: "load-failed", org: A },
    { type: "more-failed", org: A },
  ];
  for (const action of late) assert.equal(reducer(b, action), b, `${action.type} from the old organization must be ignored`);
  // ...while the new organization's own results are applied, and its badge comes only from its own data
  const b2 = run([{ type: "server-state", org: B, state: { latest_seq: 2, unread_count: 1 } }], b);
  assert.equal(b2.unread, 1);
  assert.equal(b2.latestSeq, 2);
});

test("switching A to B to A starts clean each time", () => {
  let s = run([{ type: "server-state", org: A, state: { latest_seq: 4, unread_count: 4 } }]);
  s = reducer(s, { type: "switch-org", orgId: B });
  assert.equal(s.unread, 0);
  s = run([{ type: "server-state", org: B, state: { latest_seq: 1, unread_count: 2 } }], s);
  s = reducer(s, { type: "switch-org", orgId: A });
  assert.equal(s.unread, 0);
  assert.equal(s.orgId, A);
  assert.equal(reducer(s, { type: "switch-org", orgId: A }), s);  // same organization: nothing is reset
});

test("the hook resets on an organization change, tags every result, ignores stale callbacks, and the bell is keyed by organization", () => {
  const hook = readFileSync(join(SRC, "lib/useNotifications.ts"), "utf8");
  assert.match(hook, /switch-org/);
  assert.match(hook, /raw\.orgId === \(orgId \?\? null\)/);  // the render that sees a new org shows an empty state
  const dispatches = hook.match(/dispatch\(\{ type: "(loaded|more-loaded|load-failed|more-failed|marked-read|marked-all|server-state|connection)"[^}]*\}/g) ?? [];
  assert.ok(dispatches.length >= 10);
  for (const d of dispatches) assert.match(d, /org: orgId/, d);
  assert.match(hook, /closed = true/);  // the effect cleanup closes the stream, the polls and the timers of the old organization
  assert.match(readFileSync(join(SRC, "app/dashboard/page.tsx"), "utf8"), /<NotificationBell key=\{orgId\}/);
});

// ---- real time and reconnection
test("something newer than the open list marks it stale so it refetches; the same state does not", () => {
  const ready = run([{ type: "open" }, { type: "loaded", org: A, page: page([item({ id: "a", seq: 5 })]) }]);
  assert.equal(run([{ type: "server-state", org: A, state: { latest_seq: 5, unread_count: 1 } }], ready).stale, false);
  assert.equal(run([{ type: "server-state", org: A, state: { latest_seq: 6, unread_count: 2 } }], ready).stale, true);
  assert.equal(run([{ type: "loaded", org: A, page: page([item({ id: "a", seq: 6 })]) }], run([{ type: "server-state", org: A, state: { latest_seq: 6, unread_count: 2 } }], ready)).stale, false);
});

test("a new notification arriving while the drawer was never opened only moves the badge", () => {
  const s = run([{ type: "server-state", org: A, state: { latest_seq: 9, unread_count: 4 } }]);
  assert.equal(s.unread, 4);
  assert.equal(s.stale, false);  // nothing loaded yet: the first open fetches anyway
});

test("connection states are tracked", () => {
  let s = run([{ type: "connection", org: A, connection: "live" }]);
  assert.equal(s.connection, "live");
  s = run([{ type: "connection", org: A, connection: "reconnecting" }], s);
  assert.equal(s.connection, "reconnecting");
  assert.equal(run([{ type: "connection", org: A, connection: "reconnecting" }], s), s);  // no pointless re-render
});

test("stream events are parsed defensively", () => {
  assert.deepEqual(parseStateEvent('{"latest_seq": 4, "unread_count": 2}'), { latest_seq: 4, unread_count: 2 });
  for (const bad of ["", "not json", "{}", '{"latest_seq":"4","unread_count":2}', '{"latest_seq":4,"unread_count":-1}', "null"]) assert.equal(parseStateEvent(bad), null, bad);
});

test("reconnect back-off starts at 5 s, doubles, and stops at 60 s", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 10].map(backoffMs), [5000, 10000, 20000, 40000, 60000, 60000]);
});

// ---- navigation
test("only relative dashboard links are followed", () => {
  for (const ok of ["/dashboard", "/dashboard/leads", "/dashboard/settings/billing", "/dashboard#credits", "/dashboard/leads?status=new"]) assert.equal(safeInternalLink(ok), ok, ok);
  for (const bad of ["https://evil.example", "//evil.example", "javascript:alert(1)", "/settings", "/dashboardx", "/dashboard/../x", "/dashboard/leads\n", "", null, undefined]) assert.equal(safeInternalLink(bad as string | null), null, String(bad));
});

test("times read naturally", () => {
  const now = Date.parse("2026-11-23T10:10:00Z");
  assert.equal(timeLabel("2026-11-23T10:09:30Z", now), "Just now");
  assert.equal(timeLabel("2026-11-23T10:00:00Z", now), "10 min ago");
  assert.equal(timeLabel("2026-11-23T07:10:00Z", now), "3 h ago");
});

// ---- placement and responsiveness (source checks: there is no DOM in this suite)
const SRC = new URL("../src", import.meta.url).pathname;
const files = (dir: string): string[] => readdirSync(dir).flatMap((f) => (statSync(join(dir, f)).isDirectory() ? files(join(dir, f)) : [join(dir, f)]));

test("the bell is mounted by the Dashboard Overview and nowhere else", () => {
  const users = files(SRC).filter((f) => /NotificationBell|useNotifications/.test(readFileSync(f, "utf8"))).map((f) => f.slice(SRC.length + 1)).sort();
  assert.deepEqual(users, ["app/dashboard/page.tsx", "components/dashboard/NotificationBell.tsx", "lib/useNotifications.ts"]);
  const page = readFileSync(join(SRC, "app/dashboard/page.tsx"), "utf8");
  assert.equal((page.match(/<NotificationBell/g) ?? []).length, 1);
});

test("no notification route, nav entry or settings entry exists", () => {
  const all = files(SRC).map((f) => f.slice(SRC.length + 1));
  assert.deepEqual(all.filter((f) => /^app\/.*notification/i.test(f)), []);
  const shellFiles = ["components/dashboard/nav.ts", "components/dashboard/DashboardShell.tsx", "lib/settingsRoutes.ts"].map((f) => readFileSync(join(SRC, f), "utf8")).join("\n");
  assert.doesNotMatch(shellFiles, /notification/i);
  assert.doesNotMatch(readFileSync(join(SRC, "components/dashboard/NotificationBell.tsx"), "utf8"), /settings\/notification/i);
});

test("notification preferences exist only inside the bell drawer: one import site, no route, no nav or settings entry", () => {
  const users = files(SRC).filter((f) => /NotificationPreferences|useNotificationPreferences/.test(readFileSync(f, "utf8"))).map((f) => f.slice(SRC.length + 1)).sort();
  assert.deepEqual(users, ["components/dashboard/NotificationBell.tsx", "components/dashboard/NotificationPreferences.tsx", "lib/api.ts", "lib/useNotificationPreferences.ts"]);
  const bell = readFileSync(join(SRC, "components/dashboard/NotificationBell.tsx"), "utf8");
  assert.equal((bell.match(/<NotificationPreferences/g) ?? []).length, 1);
  const outside = files(SRC).filter((f) => !/NotificationBell|NotificationPreferences|useNotificationPreferences|notificationPreferences|lib\/api\.ts/.test(f));
  for (const f of outside) assert.doesNotMatch(readFileSync(f, "utf8"), /notification.?preferences/i, f);
});

test("the drawer is a full-width sheet on phones and a 420 px panel from the sm breakpoint, with reduced-motion respected", () => {
  const c = readFileSync(join(SRC, "components/dashboard/NotificationBell.tsx"), "utf8");
  assert.match(c, /w-full[^"]*sm:max-w-\[420px\]/);
  assert.match(c, /motion-safe:transition-transform/);
  assert.match(c, /role="dialog"/);
  assert.match(c, /aria-modal="true"/);
  assert.match(c, /key === "Escape"/);
});
