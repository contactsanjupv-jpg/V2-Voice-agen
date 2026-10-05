"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, SubscriptionOut } from "@/lib/api";
import { useOrganization } from "@/lib/useOrganization";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";

const INPUT =
  "w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[15px] outline-none focus:border-[var(--color-ink)]";
const CARD = "rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-6";

function fmt(date: string | null) {
  return date ? new Date(date).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" }) : null;
}

export default function SettingsPage() {
  const router = useRouter();
  const { org, loading, error } = useOrganization();
  const [sub, setSub] = useState<SubscriptionOut | null>(null);
  const [subLoaded, setSubLoaded] = useState(false);
  const [billingMsg, setBillingMsg] = useState<string | null>(null);
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwMsg, setPwMsg] = useState<string | null>(null);
  const [delName, setDelName] = useState("");
  const [delPw, setDelPw] = useState("");
  const [delMsg, setDelMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!org) return;
    api
      .getSubscription(org.id)
      .then(setSub)
      .catch(() => setBillingMsg("Couldn't load your billing details."))
      .finally(() => setSubLoaded(true));
  }, [org]);

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;
  if (!org) return null;
  const orgId = org.id;
  const paying = sub !== null && ["active", "trialing", "past_due", "paused"].includes(sub.status);

  async function openPaymentUpdate() {
    setBillingMsg(null);
    try {
      const { update_payment_method_url } = await api.getBillingManage(orgId);
      if (update_payment_method_url) window.open(update_payment_method_url, "_blank", "noopener");
      else setBillingMsg("Payment update isn't available right now.");
    } catch (err) {
      setBillingMsg(err instanceof ApiError ? err.message : "Something went wrong.");
    }
  }

  async function cancel() {
    if (!window.confirm("Cancel your subscription? Your receptionist keeps working until the end of the period you've paid for.")) return;
    setBillingMsg(null);
    try {
      await api.cancelSubscription(orgId);
      setBillingMsg("Cancellation requested. It can take a minute to show here.");
    } catch (err) {
      setBillingMsg(err instanceof ApiError ? err.message : "Something went wrong.");
    }
  }

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    setPwMsg(null);
    try {
      await api.changePassword(pwCurrent, pwNew);
      setPwCurrent("");
      setPwNew("");
      setPwMsg("Password changed. Other devices have been signed out.");
    } catch (err) {
      setPwMsg(err instanceof ApiError ? err.message : "Something went wrong.");
    }
  }

  async function deleteAccount(e: React.FormEvent) {
    e.preventDefault();
    setDelMsg(null);
    try {
      await api.deleteAccount(orgId, delName, delPw);
      router.replace("/");
    } catch (err) {
      setDelMsg(err instanceof ApiError ? err.message : "Something went wrong.");
    }
  }

  return (
    <DashboardShell org={org}>
      <div className="max-w-xl space-y-6">
        <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Settings</h1>

        <section className={CARD}>
          <h2 className="text-[17px] font-semibold">Billing</h2>
          {!subLoaded ? (
            <p className="mt-2 text-[14px] text-[var(--color-ink-soft)]">Loading…</p>
          ) : paying && sub ? (
            <div className="mt-2 space-y-3 text-[14px]">
              <p>
                <span className="font-medium capitalize">{sub.plan_id}</span> plan · {sub.status.replace("_", " ")}
              </p>
              {sub.status === "past_due" && (
                <p className="text-[#7a2416]">Your last payment didn&apos;t go through. Update your payment method to keep your receptionist live.</p>
              )}
              {sub.cancel_effective_at ? (
                <p className="text-[var(--color-ink-soft)]">Cancelled — active until {fmt(sub.cancel_effective_at)}.</p>
              ) : (
                sub.current_period_end && <p className="text-[var(--color-ink-soft)]">Renews {fmt(sub.current_period_end)}.</p>
              )}
              <div className="flex flex-wrap gap-3">
                <button onClick={openPaymentUpdate} className="rounded-full border border-[var(--color-line)] px-5 py-2.5 text-[13.5px] font-medium">
                  Update payment method
                </button>
                {!sub.cancel_effective_at && (
                  <button onClick={cancel} className="rounded-full border border-[var(--color-line)] px-5 py-2.5 text-[13.5px] font-medium">
                    Cancel subscription
                  </button>
                )}
              </div>
            </div>
          ) : (
            <p className="mt-2 text-[14px] text-[var(--color-ink-soft)]">No active plan. Go live from your dashboard to choose one.</p>
          )}
          {billingMsg && <p className="mt-3 text-[13.5px] text-[var(--color-ink-soft)]">{billingMsg}</p>}
        </section>

        <section className={CARD}>
          <h2 className="text-[17px] font-semibold">Change password</h2>
          <form onSubmit={changePassword} className="mt-3 space-y-3">
            <input type="password" value={pwCurrent} onChange={(e) => setPwCurrent(e.target.value)} autoComplete="current-password" placeholder="Current password" className={INPUT} />
            <input type="password" value={pwNew} onChange={(e) => setPwNew(e.target.value)} autoComplete="new-password" placeholder="New password (at least 10 characters)" className={INPUT} />
            <button type="submit" disabled={!pwCurrent || pwNew.length < 10} className="rounded-full bg-[var(--color-ink)] px-5 py-2.5 text-[13.5px] font-medium text-[var(--color-paper)] disabled:opacity-50">
              Change password
            </button>
            {pwMsg && <p className="text-[13.5px] text-[var(--color-ink-soft)]">{pwMsg}</p>}
          </form>
        </section>

        <section className={CARD}>
          <h2 className="text-[17px] font-semibold">Delete account</h2>
          <p className="mt-1 text-[14px] text-[var(--color-ink-soft)]">
            This cancels your subscription, releases your phone number, and permanently deletes your receptionist, calls and leads.
          </p>
          <form onSubmit={deleteAccount} className="mt-3 space-y-3">
            <input value={delName} onChange={(e) => setDelName(e.target.value)} placeholder={`Type "${org.name}" to confirm`} className={INPUT} />
            <input type="password" value={delPw} onChange={(e) => setDelPw(e.target.value)} autoComplete="current-password" placeholder="Your password" className={INPUT} />
            <button type="submit" disabled={!delName || !delPw} className="rounded-full border border-[#7a2416] px-5 py-2.5 text-[13.5px] font-medium text-[#7a2416] disabled:opacity-50">
              Delete my account
            </button>
            {delMsg && <p className="text-[13.5px] text-[#7a2416]">{delMsg}</p>}
          </form>
        </section>
      </div>
    </DashboardShell>
  );
}
