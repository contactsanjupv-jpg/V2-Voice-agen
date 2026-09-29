"use client";

import { useState } from "react";
import { Globe } from "lucide-react";
import { api, ApiError, StructuredBusinessInfo } from "@/lib/api";
import { WizardActions, ErrorBanner } from "./WizardShell";

const EMPTY_INFO: StructuredBusinessInfo = {
  business_name: "",
  description: null,
  industry: null,
  services: [],
  address: null,
  phone: null,
  hours: null,
  faqs: [],
  policies: [],
};

export function StepWebsite({
  orgId,
  onImported,
}: {
  orgId: string;
  onImported: (businessId: string, info: StructuredBusinessInfo) => void;
}) {
  const [url, setUrl] = useState("");
  const [manualName, setManualName] = useState("");
  const [showManual, setShowManual] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleImport() {
    setError(null);
    if (!url.trim()) {
      setError("Enter your website address first.");
      return;
    }
    let normalized = url.trim();
    if (!/^https?:\/\//i.test(normalized)) normalized = `https://${normalized}`;

    setLoading(true);
    try {
      const result = await api.importWebsite(orgId, normalized);
      onImported(result.business_id, result.structured_info);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message || "Couldn't import that site — check the address and try again.");
      } else {
        setError("Can't reach the server right now.");
      }
    } finally {
      setLoading(false);
    }
  }

  async function handleManualCreate() {
    setError(null);
    if (!manualName.trim()) {
      setError("Enter your business name first.");
      return;
    }
    setLoading(true);
    try {
      const business = await api.createManualBusiness(orgId, manualName.trim());
      onImported(business.id, { ...EMPTY_INFO, business_name: manualName.trim() });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the server right now.");
    } finally {
      setLoading(false);
    }
  }

  if (showManual) {
    return (
      <div>
        <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">
          What&apos;s your business called?
        </h1>
        <p className="mt-2 text-[15px] text-[var(--color-ink-soft)]">
          No problem — you can fill in the rest by hand on the next screen.
        </p>

        <div className="mt-6">
          {error && <ErrorBanner message={error} />}
          <input
            type="text"
            value={manualName}
            onChange={(e) => setManualName(e.target.value)}
            placeholder="Your business name"
            className="w-full rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3.5 text-[15px] outline-none focus:border-[var(--color-ink)]"
            onKeyDown={(e) => e.key === "Enter" && handleManualCreate()}
          />
        </div>

        <WizardActions
          onBack={() => setShowManual(false)}
          onNext={handleManualCreate}
          nextLabel="Continue"
          nextLoading={loading}
        />
      </div>
    );
  }

  return (
    <div>
      <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">
        What&apos;s your website?
      </h1>
      <p className="mt-2 text-[15px] text-[var(--color-ink-soft)]">
        We&apos;ll read your services, hours, and FAQs from it. You&apos;ll review and edit everything before it&apos;s used.
      </p>

      <div className="mt-6">
        {error && <ErrorBanner message={error} />}
        <div className="flex items-center gap-3 rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3.5">
          <Globe className="h-4.5 w-4.5 flex-shrink-0 text-[var(--color-ink-soft)]" />
          <input
            type="text"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="yourbusiness.com"
            className="w-full bg-transparent text-[15px] outline-none"
            onKeyDown={(e) => e.key === "Enter" && handleImport()}
          />
        </div>
        <button
          onClick={() => setShowManual(true)}
          className="mt-3 text-[13.5px] font-medium text-[var(--color-ink-soft)] underline decoration-[var(--color-line)] underline-offset-4 hover:text-[var(--color-ink)]"
        >
          I don&apos;t have a website — enter details manually
        </button>
      </div>

      <WizardActions onNext={handleImport} nextLabel="Import my website" nextLoading={loading} />
    </div>
  );
}