"use client";

import { useEffect, useState } from "react";
import { Phone } from "lucide-react";
import { useOrganization } from "@/lib/useOrganization";
import { api, CallOut } from "@/lib/api";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";

function formatDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export default function CallsPage() {
  const { org, loading, error } = useOrganization();
  const [calls, setCalls] = useState<CallOut[]>([]);
  const [dataLoading, setDataLoading] = useState(true);
  const [dataError, setDataError] = useState<string | null>(null);

  useEffect(() => {
    if (!org) return;
    api
      .listCalls(org.id, { limit: 50 })
      .then(setCalls)
      .catch(() => setDataError("Couldn't load calls."))
      .finally(() => setDataLoading(false));
  }, [org]);

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;

  return (
    <DashboardShell org={org}>
      <h1 className="font-[family-name:var(--font-display)] text-[24px] font-bold tracking-tight">Calls</h1>

      {dataLoading && <p className="mt-6 text-[14px] text-[var(--color-ink-soft)]">Loading…</p>}
      {dataError && <p className="mt-6 text-[14px] text-[var(--color-ring)]">{dataError}</p>}

      {!dataLoading && !dataError && calls.length === 0 && (
        <div className="mt-10 flex flex-col items-center rounded-2xl border border-dashed border-[var(--color-line)] px-6 py-16 text-center">
          <Phone className="h-8 w-8 text-[var(--color-ink-soft)]" strokeWidth={1.5} />
          <h2 className="mt-4 text-[16px] font-semibold">No calls yet</h2>
          <p className="mt-1.5 max-w-xs text-[14px] text-[var(--color-ink-soft)]">
            Calls will appear here once your receptionist is active and answering.
          </p>
        </div>
      )}

      {!dataLoading && calls.length > 0 && (
        <div className="mt-6 overflow-x-auto rounded-xl border border-[var(--color-line)]">
            <table className="w-full min-w-[640px] text-left text-[13.5px]">
            <thead className="border-b border-[var(--color-line)] bg-[var(--color-paper-raised)] text-[12px] uppercase tracking-wide text-[var(--color-ink-soft)]">
              <tr>
                <th className="px-4 py-3 font-medium">Caller</th>
                <th className="px-4 py-3 font-medium">Direction</th>
                <th className="px-4 py-3 font-medium">Duration</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Summary</th>
              </tr>
            </thead>
            <tbody>
              {calls.map((c) => (
                <tr key={c.id} className="border-b border-[var(--color-line)] last:border-0">
                  <td className="px-4 py-3">{c.caller_number || "Unknown"}</td>
                  <td className="px-4 py-3 capitalize">{c.direction}</td>
                  <td className="px-4 py-3">{formatDuration(c.duration_seconds)}</td>
                  <td className="px-4 py-3">{c.status || "—"}</td>
                  <td className="max-w-xs truncate px-4 py-3 text-[var(--color-ink-soft)]">{c.summary || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </DashboardShell>
  );
}