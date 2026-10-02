"use client";

import { useEffect, useState } from "react";
import { Users } from "lucide-react";
import { useOrganization } from "@/lib/useOrganization";
import { api, LeadOut } from "@/lib/api";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";

const STATUS_OPTIONS = ["new", "contacted", "booked", "closed"];

const STATUS_COLORS: Record<string, string> = {
  new: "bg-[var(--color-ring-soft)] text-[#7a2416]",
  contacted: "bg-[#e8e2cf] text-[#5c5330]",
  booked: "bg-[var(--color-ok)]/15 text-[var(--color-ok)]",
  closed: "bg-[var(--color-line)] text-[var(--color-ink-soft)]",
};

export default function LeadsPage() {
  const { org, loading, error } = useOrganization();
  const [leads, setLeads] = useState<LeadOut[]>([]);
  const [dataLoading, setDataLoading] = useState(true);
  const [dataError, setDataError] = useState<string | null>(null);
  const [updatingId, setUpdatingId] = useState<string | null>(null);

  useEffect(() => {
    if (!org) return;
    api
      .listLeads(org.id, { limit: 50 })
      .then(setLeads)
      .catch(() => setDataError("Couldn't load leads."))
      .finally(() => setDataLoading(false));
  }, [org]);

  async function handleStatusChange(leadId: string, newStatus: string) {
    if (!org) return;
    setUpdatingId(leadId);
    try {
      const updated = await api.updateLeadStatus(org.id, leadId, newStatus);
      setLeads((prev) => prev.map((l) => (l.id === leadId ? updated : l)));
    } catch {
      // Leave the lead as-is on failure.
    } finally {
      setUpdatingId(null);
    }
  }

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;

  return (
    <DashboardShell org={org}>
      <h1 className="font-[family-name:var(--font-display)] text-[24px] font-bold tracking-tight">Leads</h1>

      {dataLoading && <p className="mt-6 text-[14px] text-[var(--color-ink-soft)]">Loading…</p>}
      {dataError && <p className="mt-6 text-[14px] text-[var(--color-ring)]">{dataError}</p>}

      {!dataLoading && !dataError && leads.length === 0 && (
        <div className="mt-10 flex flex-col items-center rounded-2xl border border-dashed border-[var(--color-line)] px-6 py-16 text-center">
          <Users className="h-8 w-8 text-[var(--color-ink-soft)]" strokeWidth={1.5} />
          <h2 className="mt-4 text-[16px] font-semibold">No leads yet</h2>
          <p className="mt-1.5 max-w-xs text-[14px] text-[var(--color-ink-soft)]">
            Leads your receptionist captures on calls will show up here.
          </p>
        </div>
      )}

      {!dataLoading && leads.length > 0 && (
        <div className="mt-6 space-y-2">
          {leads.map((lead) => (
            <div
              key={lead.id}
              className="flex items-center justify-between rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3.5"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[14.5px] font-medium">{lead.name || "Unnamed lead"}</span>
                  {lead.phone && <span className="text-[13px] text-[var(--color-ink-soft)]">{lead.phone}</span>}
                </div>
                {lead.reason && <p className="mt-0.5 truncate text-[13px] text-[var(--color-ink-soft)]">{lead.reason}</p>}
              </div>

              <select
                value={lead.status}
                onChange={(e) => handleStatusChange(lead.id, e.target.value)}
                disabled={updatingId === lead.id}
                className={`ml-4 rounded-full border-0 px-3 py-1.5 text-[12.5px] font-medium capitalize outline-none ${STATUS_COLORS[lead.status] || ""}`}
              >
                {STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
          ))}
        </div>
      )}
    </DashboardShell>
  );
}