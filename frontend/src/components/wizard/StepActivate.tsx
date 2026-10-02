"use client";

import { useState } from "react";
import Link from "next/link";
import { CheckCircle2 } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { ErrorBanner } from "./WizardShell";

export function StepActivate({
  orgId,
  agentId,
  phoneNumberId,
  onBack,
}: {
  orgId: string;
  agentId: string;
  phoneNumberId: string;
  onBack: () => void;
}) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activated, setActivated] = useState(false);

  async function handleActivate() {
    setError(null);
    setLoading(true);
    try {
      await api.activate(orgId, agentId, phoneNumberId);
      setActivated(true);
    } catch (err) {
      setError(
          err instanceof ApiError
          ? err.message
         : "Can't reach the server right now."
    );
    } finally {
      setLoading(false);
    }
  }

  if (activated) {
    return (
      <div className="flex flex-col items-center py-12 text-center">
        <CheckCircle2 className="h-12 w-12 text-[var(--color-ok)]" strokeWidth={1.5} />
        <h1 className="mt-5 font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">
          You&apos;re live
        </h1>
                <p className="mt-2 max-w-sm text-[15px] text-[var(--color-ink-soft)]">
          Your receptionist is now answering calls.
        </p>
        <Link
          href="/dashboard"
          className="mt-6 rounded-full bg-[var(--color-ink)] px-6 py-3 text-[14px] font-medium text-[var(--color-paper)]"
        >
          Go to dashboard
        </Link>
      </div>
    );
  }

  return (
    <div>
      <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Ready to activate</h1>
      <p className="mt-2 text-[15px] text-[var(--color-ink-soft)]">
        This connects your phone number to your receptionist. Real calls will start going through it right away.
      </p>

      <div className="mt-6">
        {error && <ErrorBanner message={error} />}
      </div>

      <div className="mt-8 flex items-center justify-between">
        <button onClick={onBack} className="text-[14px] font-medium text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
          Back
        </button>
        <button
          onClick={handleActivate}
          disabled={loading}
          className="rounded-full bg-[var(--color-ring)] px-7 py-3.5 text-[15px] font-medium text-white transition-transform hover:scale-[1.02] active:scale-[0.98] disabled:opacity-60"
        >
          {loading ? "Activating…" : "Activate"}
        </button>
      </div>
    </div>
  );
}
