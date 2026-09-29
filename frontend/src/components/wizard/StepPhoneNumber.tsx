"use client";

import { useState } from "react";
import { Phone, CheckCircle2 } from "lucide-react";
import { api, ApiError, PhoneNumberOut } from "@/lib/api";
import { WizardActions, ErrorBanner } from "./WizardShell";

export function StepPhoneNumber({
  orgId,
  onPurchased,
  onBack,
}: {
  orgId: string;
  onPurchased: (phoneNumberId: string) => void;
  onBack: () => void;
}) {
  const [areaCode, setAreaCode] = useState("");
  const [purchasing, setPurchasing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [purchased, setPurchased] = useState<PhoneNumberOut | null>(null);

  async function handleGetNumber() {
    setError(null);
    setPurchasing(true);
    try {
      const number = await api.purchasePhoneNumber(orgId, "US", areaCode.trim() || undefined);
      setPurchased(number);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${err.message} — this needs a real Retell API key configured on the backend.`
          : "Can't reach the server right now."
      );
    } finally {
      setPurchasing(false);
    }
  }

  return (
    <div>
      <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Get a phone number</h1>
      <p className="mt-2 text-[15px] text-[var(--color-ink-soft)]">
        This is the number your customers will call. We&apos;ll get you a real one — the area code below
        is a preference, not a guarantee.
      </p>

      <div className="mt-6">
        {error && <ErrorBanner message={error} />}

        {!purchased ? (
          <div className="flex gap-2">
            <input
              value={areaCode}
              onChange={(e) => setAreaCode(e.target.value)}
              placeholder="Preferred area code (optional)"
              className="w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[14.5px] outline-none focus:border-[var(--color-ink)]"
            />
            <button
              onClick={handleGetNumber}
              disabled={purchasing}
              className="whitespace-nowrap rounded-lg bg-[var(--color-ink)] px-5 text-[14px] font-medium text-[var(--color-paper)] disabled:opacity-60"
            >
              {purchasing ? "Getting your number…" : "Get a number"}
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-3 rounded-xl border border-[var(--color-ok)]/30 bg-[var(--color-ok)]/10 px-4 py-3.5">
            <CheckCircle2 className="h-5 w-5 flex-shrink-0 text-[var(--color-ok)]" />
            <div>
              <div className="flex items-center gap-2 text-[15px] font-medium">
                <Phone className="h-3.5 w-3.5" />
                {purchased.number}
              </div>
              {purchased.monthly_cost_cents != null && (
                <div className="text-[13px] text-[var(--color-ink-soft)]">
                  ${(purchased.monthly_cost_cents / 100).toFixed(2)}/mo
                </div>
              )}
            </div>
          </div>
        )}
      </div>

      <WizardActions
        onBack={onBack}
        onNext={() => purchased && onPurchased(purchased.id)}
        nextDisabled={!purchased}
      />
    </div>
  );
}