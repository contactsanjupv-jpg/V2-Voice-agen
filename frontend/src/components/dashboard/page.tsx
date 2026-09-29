"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { PhoneCall, Plus } from "lucide-react";
import { useOrganization } from "@/lib/useOrganization";
import { api, CallOut, LeadOut } from "@/lib/api";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";

export default function DashboardOverview() {
  const { org, loading, error } = useOrganization();
  const [calls, setCalls] = useState<CallOut[]>([]);
  const [leads, setLeads] = useState<LeadOut[]>([]);
  const [dataLoading, setDataLoading] = useState(true);

  useEffect(() => {
    if (!org) return;
    Promise.all([api.listCalls(org.id, { limit: 5 }), api.listLeads(org.id, { limit: 5 })])
      .then(([callsData, leadsData]) => {
        setCalls(callsData);
        setLeads(leadsData);
      })
      .catch(() => {
        // Non-fatal for the overview — empty state below covers this.
      })
      .finally(() => setDataLoading(false));
  }, [org]);

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;

  return (
    <DashboardShell org={org}>
      <h1 className="font-[family-name:var(--font-display)] text-[24px] font-bold tracking-tight">Overview</h1>
      <p className="mt-1 text-[14.5px] text-[var(--color-ink-soft)]">
        {org?.name} · {dataLoading ? "loading…" : `${calls.length} recent calls, ${leads.length} recent leads`}
      </p>

      {!dataLoading && calls.length === 0 && leads.length === 0 && (
        <div className="mt-10 flex flex-col items-center rounded-2xl border border-dashed border-[var(--color-line)] px-6 py-16 text-center">
          <PhoneCall className="h-8 w-8 text-[var(--color-ink-soft)]" strokeWidth={1.5} />
          <h2 className="mt-4 text-[16px] font-semibold">No calls yet</h2>
          <p className="mt-1.5 max-w-xs text-[14px] text-[var(--color-ink-soft)]">
            Once your receptionist is active and takes its first call, it&apos;ll show up here.
          </p>
          <Link
            href="/onboarding"
            className="mt-5 flex items-center gap-1.5 rounded-full bg-[var(--color-ink)] px-5 py-2.5 text-[13.5px] font-medium text-[var(--color-paper)]"
          >
            <Plus className="h-3.5 w-3.5" />
            Set up your receptionist
          </Link>
        </div>
      )}

      {!dataLoading && (calls.length > 0 || leads.length > 0) && (
        <div className="mt-8 grid gap-6 md:grid-cols-2">
          <div>
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-[15px] font-semibold">Recent calls</h2>
              <Link href="/dashboard/calls" className="text-[13px] text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                View all →
              </Link>
            </div>
            <div className="space-y-2">
              {calls.map((c) => (
                <div key={c.id} className="rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3">
                  <div className="flex items-center justify-between text-[13.5px]">
                    <span className="font-medium">{c.caller_number || "Unknown caller"}</span>
                    <span className="text-[var(--color-ink-soft)]">{c.status || "—"}</span>
                  </div>
                  {c.summary && <p className="mt-1 truncate text-[13px] text-[var(--color-ink-soft)]">{c.summary}</p>}
                </div>
              ))}
            </div>
          </div>

          <div>
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-[15px] font-semibold">Recent leads</h2>
              <Link href="/dashboard/leads" className="text-[13px] text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
                View all →
              </Link>
            </div>
            <div className="space-y-2">
              {leads.map((l) => (
                <div key={l.id} className="rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3">
                  <div className="flex items-center justify-between text-[13.5px]">
                    <span className="font-medium">{l.name || "Unnamed lead"}</span>
                    <span className="text-[var(--color-ink-soft)]">{l.status}</span>
                  </div>
                  {l.reason && <p className="mt-1 truncate text-[13px] text-[var(--color-ink-soft)]">{l.reason}</p>}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </DashboardShell>
  );
}