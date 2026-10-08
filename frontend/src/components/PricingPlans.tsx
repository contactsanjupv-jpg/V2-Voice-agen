"use client";

import Link from "next/link";
import { Check } from "lucide-react";
import { formatPrice } from "@/lib/billing";
import { usePlans } from "@/lib/usePlans";

/** Public plan cards. Names, prices and features all come from the backend plan catalog — nothing is hardcoded here. */
export function PricingPlans() {
  const { plans, loading, failed, reload } = usePlans();

  if (failed) {
    return (
      <div className="mx-auto max-w-md text-center">
        <p className="text-[15px] text-[var(--color-ink-soft)]">We couldn&apos;t load our plans right now.</p>
        <button
          onClick={reload}
          className="mt-4 rounded-full border border-[var(--color-line)] px-5 py-2.5 text-[13.5px] font-medium hover:bg-[var(--color-line)]"
        >
          Try again
        </button>
      </div>
    );
  }

  if (loading || plans === null) {
    return (
      <div className="grid animate-pulse gap-6 md:grid-cols-2" aria-busy="true" aria-label="Loading plans">
        {[0, 1].map((i) => (
          <div key={i} className="h-72 rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)]" />
        ))}
      </div>
    );
  }

  return (
    <div className="mx-auto grid max-w-3xl gap-6 md:grid-cols-2">
      {plans.map((plan) => {
        const price = formatPrice(plan.price);
        return (
          <div key={plan.id} className="flex flex-col rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-7">
            <h3 className="text-[16px] font-semibold">{plan.name}</h3>
            <div className="mt-3 flex items-baseline gap-1">
              {price ? (
                <>
                  <span className="font-[family-name:var(--font-display)] text-[36px] font-bold">{price.amount}</span>
                  <span className="text-[14px] text-[var(--color-ink-soft)]">{price.cadence}</span>
                </>
              ) : (
                <span className="text-[15px] text-[var(--color-ink-soft)]">Price shown at checkout</span>
              )}
            </div>

            <ul className="mt-6 flex-1 space-y-2.5">
              {plan.features.map((f) => (
                <li key={f.id} className="flex items-start gap-2 text-[14px]">
                  <Check className="mt-0.5 h-4 w-4 flex-shrink-0 text-[var(--color-ok)]" aria-hidden />
                  {f.label}
                </li>
              ))}
            </ul>

            <Link
              href="/get-started"
              className="mt-7 rounded-full bg-[var(--color-ink)] px-5 py-3 text-center text-[14px] font-medium text-[var(--color-paper)] transition-transform hover:scale-[1.02] active:scale-[0.98]"
            >
              Get started
            </Link>
          </div>
        );
      })}
    </div>
  );
}