import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import type { AgentOut, PhoneNumberOut, SubscriptionOut } from "../src/lib/api.ts";
import { formatDate } from "../src/lib/billing.ts";
import { describeSubscription } from "../src/lib/billingState.ts";
import { receptionistStatus } from "../src/lib/receptionistStatus.ts";

const NOW = Date.parse("2026-10-12T12:00:00Z");
const DAY = 24 * 60 * 60 * 1000;
const sub = (over: Partial<SubscriptionOut> = {}): SubscriptionOut => ({
  plan_id: "starter", plan_name: "Starter", status: "active", entitled: true, features: [],
  current_period_start: null, current_period_end: "2026-11-01T00:00:00Z", cancel_effective_at: null, service_ends_at: null, ...over,
});
const agent = (over: Partial<AgentOut> = {}): AgentOut => ({ id: "a", name: "Ava", status: "active", retell_agent_id: "r", synced: true, ...over });
const phone: PhoneNumberOut = { id: "p", number: "+13055551234", area_code: "305", country: "US", status: "active" };

// past_due inside / past the 3-day grace, relative to NOW (receptionistStatus takes `now`)
const inGrace = (over: Partial<SubscriptionOut> = {}) =>
  sub({ status: "past_due", entitled: false, service_ends_at: new Date(NOW + 2 * DAY).toISOString(), ...over });
const pastGrace = (over: Partial<SubscriptionOut> = {}) =>
  sub({ status: "past_due", entitled: false, service_ends_at: new Date(NOW - DAY).toISOString(), ...over });

// ---------------- past-due banner (describeSubscription uses the real clock, so use a date well ahead of it) ----------------

test("past-due banner inside the grace says service may pause sooner when credit runs out", () => {
  const ends = new Date(Date.now() + 2 * DAY).toISOString();
  const info = describeSubscription(sub({ status: "past_due", entitled: false, service_ends_at: ends }));
  assert.equal(info.label, "Payment needed");
  assert.equal(info.tone, "bad");
  assert.ok(info.detail.includes(formatDate(ends)!), "still names the date the grace ends");
  assert.match(info.detail, /sooner/i);
  assert.match(info.detail, /credit/i);
  assert.match(info.detail, /payment/i);
  assert.doesNotMatch(info.detail, /stays live until/i, "must not promise service until the date");
});

test("past-due banner never suggests that buying credit fixes a past-due plan", () => {
  const ends = new Date(Date.now() + 2 * DAY).toISOString();
  const { detail } = describeSubscription(sub({ status: "past_due", entitled: false, service_ends_at: ends }));
  assert.doesNotMatch(detail, /top.?up|buy|add credit|purchase/i);
});

test("past-due banner after the grace is unchanged: it has stopped", () => {
  const ended = new Date(Date.now() - DAY).toISOString();
  const info = describeSubscription(sub({ status: "past_due", entitled: false, service_ends_at: ended }));
  assert.match(info.detail, /stopped answering calls/);
  assert.match(info.detail, /Update your payment method/);
});

test("an active plan's banner is unchanged", () => {
  const info = describeSubscription(sub());
  assert.equal(info.headline, "Your plan is active");
  assert.equal(info.tone, "ok");
});

// ---------------- dashboard / sidebar status ----------------

test("past-due customer with a paused receptionist sees Paused + Fix billing, not 'Ready to go live'", () => {
  for (const s of [inGrace(), pastGrace()]) {
    const r = receptionistStatus({ agent: agent({ status: "paused" }), phone, sub: s, now: NOW });
    assert.equal(r.state, "billing");
    assert.equal(r.label, "Paused");
    assert.equal(r.tone, "bad");
    assert.match(r.detail, /payment is overdue/i);
    assert.match(r.detail, /isn't answering calls/);
    assert.deepEqual(r.action, { label: "Fix billing", href: "/dashboard/settings" });
    assert.notEqual(r.label, "Ready to go live");
  }
});

test("the paused past-due state does not promise that credit or a top-up brings it back", () => {
  const r = receptionistStatus({ agent: agent({ status: "paused" }), phone, sub: inGrace(), now: NOW });
  assert.doesNotMatch(r.detail, /top.?up|buy|add credit|purchase/i);
});

test("Fix billing points at the existing Settings page, which hosts the payment-update flow", () => {
  const r = receptionistStatus({ agent: agent({ status: "paused" }), phone, sub: inGrace(), now: NOW });
  assert.equal(r.action?.href, "/dashboard/settings");
  const page = readFileSync(new URL("../src/app/dashboard/settings/page.tsx", import.meta.url), "utf8");
  assert.match(page, /<BillingPanel /);
  const panel = readFileSync(new URL("../src/components/billing/BillingPanel.tsx", import.meta.url), "utf8");
  assert.match(panel, /Update payment method/);
  assert.match(panel, /getBillingManage/);
});

test("other states are unchanged: active agent in grace, active agent past grace, never-live agent, healthy plan", () => {
  const live = receptionistStatus({ agent: agent(), phone, sub: inGrace(), now: NOW });
  assert.equal(live.state, "live");
  assert.equal(live.tone, "warn");

  const stopped = receptionistStatus({ agent: agent(), phone, sub: pastGrace(), now: NOW });
  assert.equal(stopped.state, "billing");
  assert.equal(stopped.label, "Not answering calls");
  assert.equal(stopped.detail, "Your payment is overdue.");

  const draft = receptionistStatus({ agent: agent({ status: "draft" }), phone, sub: inGrace(), now: NOW });
  assert.equal(draft.state, "ready", "a receptionist that never went live is not accused of being paused");

  const healthyPaused = receptionistStatus({ agent: agent({ status: "paused" }), phone, sub: sub(), now: NOW });
  assert.equal(healthyPaused.state, "ready", "a paused agent on a healthy plan keeps the Go live action");
  assert.deepEqual(healthyPaused.action, { label: "Go live", href: "/dashboard/go-live" });

  const ok = receptionistStatus({ agent: agent(), phone, sub: sub(), now: NOW });
  assert.equal(ok.state, "live");
  assert.equal(ok.tone, "ok");
});

test("no billing claim is made when the subscription couldn't be read", () => {
  const r = receptionistStatus({ agent: agent({ status: "paused" }), phone, sub: undefined, now: NOW });
  assert.equal(r.state, "ready");
});
