"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, describeError } from "@/lib/api";
import { useOrganization } from "@/lib/useOrganization";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";
import { BillingPanel } from "@/components/billing/BillingPanel";

const INPUT =
  "w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[15px] outline-none focus:border-[var(--color-ink)]";
const CARD = "rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-6";

export default function SettingsPage() {
  const router = useRouter();
  const { org, loading, error } = useOrganization();
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwMsg, setPwMsg] = useState<string | null>(null);
  const [delName, setDelName] = useState("");
  const [delPw, setDelPw] = useState("");
  const [delMsg, setDelMsg] = useState<string | null>(null);

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;
  if (!org) return null;
  const orgId = org.id;

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    setPwMsg(null);
    try {
      await api.changePassword(pwCurrent, pwNew);
      setPwCurrent("");
      setPwNew("");
      setPwMsg("Password changed. Other devices have been signed out.");
    } catch (err) {
      setPwMsg(describeError(err));
    }
  }

  async function deleteAccount(e: React.FormEvent) {
    e.preventDefault();
    setDelMsg(null);
    try {
      await api.deleteAccount(orgId, delName, delPw);
      router.replace("/");
    } catch (err) {
      setDelMsg(describeError(err));
    }
  }

  return (
    <DashboardShell org={org}>
      <div className="max-w-2xl space-y-6">
        <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Settings</h1>

        <BillingPanel orgId={orgId} role={org.role} />

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