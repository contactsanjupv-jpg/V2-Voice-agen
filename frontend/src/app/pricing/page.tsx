import Link from "next/link";
import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import { Check } from "lucide-react";

/**
 * Placeholder tiers/prices — Sanju, replace these with real numbers before
 * this goes live. Structure and copy are real; the dollar figures are not.
 */
const PLANS = [
  {
    name: "Starter",
    price: "$79",
    period: "/month",
    detail: "One phone number, enough minutes for a single location finding its footing.",
    features: ["1 phone number", "1 receptionist", "Lead capture", "Call summaries on your dashboard"],
    highlighted: false,
  },
  {
    name: "Growth",
    price: "$199",
    period: "/month",
    detail: "For a business whose phone rings all day and can't afford to miss any of it.",
    features: ["1 phone number", "1 receptionist", "Everything in Starter", "Higher monthly minutes", "Priority support"],
    highlighted: true,
  },
  {
    name: "Multi-location",
    price: "Talk to us",
    period: "",
    detail: "Multiple locations or numbers under one account.",
    features: ["Multiple phone numbers", "Per-location configuration", "Consolidated billing"],
    highlighted: false,
  },
];

export default function Pricing() {
  return (
    <div className="flex min-h-screen flex-col">
      <Nav />
      <main className="flex-1">
        <section className="mx-auto max-w-3xl px-6 pb-8 pt-16 text-center md:pt-24">
          <h1 className="font-[family-name:var(--font-display)] text-[40px] font-bold leading-[1.1] tracking-tight md:text-[48px]">
            Simple pricing, no surprises.
          </h1>
          <p className="mt-5 text-[17px] leading-relaxed text-[var(--color-ink-soft)]">
            One flat monthly price. No per-call fees to worry about.
          </p>
        </section>

        <section className="mx-auto max-w-5xl px-6 py-16">
          <div className="grid gap-6 md:grid-cols-3">
            {PLANS.map((plan) => (
              <div
                key={plan.name}
                className={`flex flex-col rounded-2xl border p-7 ${
                  plan.highlighted
                    ? "border-[var(--color-ink)] bg-[var(--color-ink)] text-[var(--color-paper)]"
                    : "border-[var(--color-line)] bg-[var(--color-paper-raised)]"
                }`}
              >
                <h3 className="text-[16px] font-semibold">{plan.name}</h3>
                <div className="mt-3 flex items-baseline gap-1">
                  <span className="font-[family-name:var(--font-display)] text-[36px] font-bold">{plan.price}</span>
                  <span className={`text-[14px] ${plan.highlighted ? "text-[#a8abb5]" : "text-[var(--color-ink-soft)]"}`}>
                    {plan.period}
                  </span>
                </div>
                <p className={`mt-3 text-[14px] leading-relaxed ${plan.highlighted ? "text-[#c7c9d1]" : "text-[var(--color-ink-soft)]"}`}>
                  {plan.detail}
                </p>

                <ul className="mt-6 flex-1 space-y-2.5">
                  {plan.features.map((f) => (
                    <li key={f} className="flex items-start gap-2 text-[14px]">
                      <Check className={`mt-0.5 h-4 w-4 flex-shrink-0 ${plan.highlighted ? "text-[var(--color-ring)]" : "text-[var(--color-ok)]"}`} />
                      {f}
                    </li>
                  ))}
                </ul>

                <Link
                  href="/get-started"
                  className={`mt-7 rounded-full px-5 py-3 text-center text-[14px] font-medium transition-transform hover:scale-[1.02] active:scale-[0.98] ${
                    plan.highlighted
                      ? "bg-[var(--color-ring)] text-white"
                      : "bg-[var(--color-ink)] text-[var(--color-paper)]"
                  }`}
                >
                  Get started
                </Link>
              </div>
            ))}
          </div>
        </section>
      </main>
      <Footer />
    </div>
  );
}
