
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import type {
  AgentOut,
  CreditsOut,
  PhoneNumberOut,
  SubscriptionOut,
} from "../src/lib/api.ts";
import { describeSubscription } from "../src/lib/billingState.ts";
import { formatDate } from "../src/lib/billing.ts";
import * as receptionist from "../src/lib/receptionistStatus.ts";
import { receptionistStatus } from "../src/lib/receptionistStatus.ts";
import { attentionItems } from "../src/lib/dashboard.ts";
import { SETTINGS_BILLING_HREF } from "../src/lib/settingsRoutes.ts";

const NOW = Date.now();
const DAY = 86_400_000;

const sub = (over: Partial<SubscriptionOut> = {}): SubscriptionOut => ({
  plan_id: "starter",
  plan_name: "Starter",
  status: "active",
  entitled: true,
  features: [],
  current_period_start: null,
  current_period_end: "2026-11-01T00:00:00Z",
  cancel_effective_at: null,
  service_ends_at: null,
  ...over,
});

const pastDueInGrace = () =>
  sub({
    status: "past_due",
    entitled: false,
    service_ends_at: new Date(NOW + 2 * DAY).toISOString(),
  });

const pastDueExpired = () =>
  sub({
    status: "past_due",
    entitled: false,
    service_ends_at: new Date(NOW - DAY).toISOString(),
  });

const agent = (over: Partial<AgentOut> = {}): AgentOut => ({
  id: "a",
  name: "Ava",
  status: "active",
  retell_agent_id: "r",
  synced: true,
  ...over,
});

const phone: PhoneNumberOut = {
  id: "p",
  number: "+13055551234",
  area_code: "305",
  country: "US",
  status: "active",
};

const credits: CreditsOut = {
  entitled: false,
  plan_name: "Starter",
  period_start: null,
  period_end: null,
  included_granted_seconds: 10800,
  included_used_seconds: 10800,
  included_remaining_seconds: 0,
  purchased_balance_seconds: 0,
  available_seconds: 0,
  status: "empty",
  calls_handled: 0,
  topup_min_minor: 2000,
  topup_max_minor: 50000,
  topup_cents_per_minute: 45,
  free_test_calls_remaining: 0,
  when_credits_run_out: "",
};

// ---- The past-due banner never promises service the credit cut-off can take away ----

test("past-due banner inside the grace does not promise the receptionist stays live", () => {
  const info = describeSubscription(pastDueInGrace());

  assert.equal(info.label, "Payment needed");
  assert.doesNotMatch(info.detail, /stays live/i);
  assert.match(info.detail, /pause sooner/i);
  assert.match(info.detail, /credit/i);
});

test("past-due banner still names the latest possible end of service", () => {
  const s = pastDueInGrace();

  assert.ok(formatDate(s.service_ends_at));
  assert.ok(
    describeSubscription(s).detail.includes(formatDate(s.service_ends_at)!),
  );
});

test("past-due banner never suggests that buying credit fixes a past-due plan", () => {
  const { detail } = describeSubscription(pastDueInGrace());

  assert.doesNotMatch(detail, /top.?up|buy|add credit|purchase/i);
});

test("past-due banner after the grace says it has stopped, and does not offer top-ups as the fix", () => {
  const info = describeSubscription(pastDueExpired());

  assert.match(info.detail, /stopped answering calls/i);
  assert.doesNotMatch(info.detail, /top.?up|buy.*credit/i);
});

test("active, ending and ended wording is unchanged", () => {
  assert.equal(describeSubscription(sub()).headline, "Your plan is active");

  assert.equal(
    describeSubscription(
      sub({ cancel_effective_at: "2026-11-01T00:00:00Z" }),
    ).label,
    "Ending",
  );

  assert.equal(
    describeSubscription(
      sub({ status: "canceled", entitled: false }),
    ).label,
    "Ended",
  );
});

// ---- The receptionist status never says ready/live when the sweeper paused it ----

test("past-due + agent paused inside the grace is Paused with Fix billing, never Ready to go live", () => {
  const s = receptionistStatus({
    agent: agent({ status: "paused" }),
    phone,
    sub: pastDueInGrace(),
    now: NOW,
  });

  assert.equal(s.state, "billing");
  assert.equal(s.label, "Paused");
  assert.equal(s.tone, "bad");
  assert.deepEqual(s.action, {
    label: "Fix billing",
    href: SETTINGS_BILLING_HREF,
  });
  assert.match(s.detail, /overdue/i);
  assert.match(s.detail, /isn't answering calls/i);
});

test("past-due + agent paused after the grace is also Paused", () => {
  const s = receptionistStatus({
    agent: agent({ status: "paused" }),
    phone,
    sub: pastDueExpired(),
    now: NOW,
  });

  assert.equal(s.label, "Paused");
  assert.equal(s.state, "billing");
});

test("past-due + agent paused ignores an unsaved-changes flag: the money problem comes first", () => {
  const s = receptionistStatus({
    agent: agent({ status: "paused", synced: false }),
    phone,
    sub: pastDueInGrace(),
    now: NOW,
  });

  assert.equal(s.label, "Paused");
});

test("the paused past-due state does not promise that credit or a top-up brings it back", () => {
  const s = receptionistStatus({
    agent: agent({ status: "paused" }),
    phone,
    sub: pastDueInGrace(),
    now: NOW,
  });

  assert.doesNotMatch(s.detail, /top.?up|buy|add credit|purchase/i);
});

test("past-due inside the grace with the agent still active is Live with a payment warning", () => {
  const s = receptionistStatus({
    agent: agent(),
    phone,
    sub: pastDueInGrace(),
    now: NOW,
  });

  assert.equal(s.state, "live");
  assert.equal(s.tone, "warn");
  assert.equal(s.action?.href, SETTINGS_BILLING_HREF);
});

test("past-due after the grace with the agent still active is Not answering calls", () => {
  const s = receptionistStatus({
    agent: agent(),
    phone,
    sub: pastDueExpired(),
    now: NOW,
  });

  assert.equal(s.label, "Not answering calls");
  assert.equal(s.tone, "bad");
});

test("a paid-up customer whose agent is paused still sees Ready to go live", () => {
  const s = receptionistStatus({
    agent: agent({ status: "paused" }),
    phone,
    sub: sub(),
    now: NOW,
  });

  assert.equal(s.state, "ready");
  assert.equal(s.label, "Ready to go live");
  assert.deepEqual(s.action, {
    label: "Go live",
    href: "/dashboard/go-live",
  });
});

test("an agent that was never live is not described as paused for billing", () => {
  const s = receptionistStatus({
    agent: agent({ status: "testing" }),
    phone,
    sub: pastDueInGrace(),
    now: NOW,
  });

  assert.equal(s.label, "Ready to go live");
});

test("an unreadable subscription makes no billing claim about a paused agent", () => {
  const s = receptionistStatus({
    agent: agent({ status: "paused" }),
    phone,
    sub: undefined,
    now: NOW,
  });

  assert.equal(s.label, "Ready to go live");
  assert.equal(s.state, "ready");
});

// ---- The business-number card does not claim callers reach a receptionist that is not answering ----

test("number caption only claims callers reach the receptionist when it is not stopped for billing", () => {
  const paused = receptionistStatus({
    agent: agent({ status: "paused" }),
    phone,
    sub: pastDueInGrace(),
    now: NOW,
  });

  const stopped = receptionistStatus({
    agent: agent(),
    phone,
    sub: pastDueExpired(),
    now: NOW,
  });

  const live = receptionistStatus({
    agent: agent(),
    phone,
    sub: sub(),
    now: NOW,
  });

  const caption = (
    receptionist as unknown as {
      numberCaption: (s: unknown) => string;
    }
  ).numberCaption;

  assert.equal(typeof caption, "function");
  assert.equal(caption(live), "Callers reach your receptionist here.");
  assert.equal(caption(null), "Callers reach your receptionist here.");

  for (const s of [paused, stopped]) {
    assert.doesNotMatch(caption(s), /callers reach/i);
    assert.match(caption(s), /not answering|isn't answering/i);
  }
});

// ---- The dashboard attention list shows the billing problem once, honestly ----

test("dashboard lists one billing item for a paused past-due receptionist, with the Paused label", () => {
  const status = receptionistStatus({
    agent: agent({ status: "paused" }),
    phone,
    sub: pastDueInGrace(),
    now: NOW,
  });

  const items = attentionItems({
    status,
    sub: pastDueInGrace(),
    agent: agent({ status: "paused" }),
    phone,
    credits,
    awaitingLeads: 0,
    oldestAwaitingAt: null,
    now: NOW,
  });

  const billing = items.filter(
    (i) => i.id === "receptionist" || i.id === "plan",
  );

  assert.equal(billing.length, 1);
  assert.equal(billing[0].title, "Paused");
  assert.equal(billing[0].tone, "bad");
  assert.equal(billing[0].cta.href, SETTINGS_BILLING_HREF);
});

// ---- Fix billing leads to the existing payment-update flow ----

test("Fix billing points at the existing Settings page hosting the payment-update flow", () => {
  const s = receptionistStatus({
    agent: agent({ status: "paused" }),
    phone,
    sub: pastDueInGrace(),
    now: NOW,
  });

  assert.equal(s.action?.href, SETTINGS_BILLING_HREF);

  const page = readFileSync(
    new URL("../src/app/dashboard/settings/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(page, /<BillingPanel /);

  const panel = readFileSync(
    new URL("../src/components/billing/BillingPanel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(panel, /Update payment method/);
  assert.match(panel, /getBillingManage/);
});

// ---- Other states remain unchanged ----

test("other states are unchanged: active agent in grace, active agent past grace, never-live agent, healthy plan", () => {
  const live = receptionistStatus({
    agent: agent(),
    phone,
    sub: pastDueInGrace(),
    now: NOW,
  });

  assert.equal(live.state, "live");
  assert.equal(live.tone, "warn");

  const stopped = receptionistStatus({
    agent: agent(),
    phone,
    sub: pastDueExpired(),
    now: NOW,
  });

  assert.equal(stopped.label, "Not answering calls");
  assert.equal(stopped.detail, "Your payment is overdue.");

  const draft = receptionistStatus({
    agent: agent({ status: "testing" }),
    phone,
    sub: pastDueInGrace(),
    now: NOW,
  });

  assert.equal(draft.state, "ready");

  const healthyPaused = receptionistStatus({
    agent: agent({ status: "paused" }),
    phone,
    sub: sub(),
    now: NOW,
  });

  assert.equal(healthyPaused.state, "ready");
  assert.deepEqual(healthyPaused.action, {
    label: "Go live",
    href: "/dashboard/go-live",
  });

  const ok = receptionistStatus({
    agent: agent(),
    phone,
    sub: sub(),
    now: NOW,
  });

  assert.equal(ok.state, "live");
  assert.equal(ok.tone, "ok");
});
