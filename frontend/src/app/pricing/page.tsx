import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import { PricingPlans } from "@/components/PricingPlans";

export default function Pricing() {
  return (
    <div className="flex min-h-screen flex-col">
      <Nav />
      <main className="flex-1">
        <section className="mx-auto max-w-3xl px-6 pb-8 pt-16 text-center md:pt-24">
          <h1 className="font-[family-name:var(--font-display)] text-[40px] font-bold leading-[1.1] tracking-tight md:text-[48px]">
            Simple pricing.
          </h1>
          <p className="mt-5 text-[17px] leading-relaxed text-[var(--color-ink-soft)]">
            Build and test your receptionist first. Choose a plan when you&apos;re ready to go live, and cancel any time from your dashboard.
          </p>
        </section>

        <section className="mx-auto max-w-5xl px-6 py-16">
          <PricingPlans />
        </section>
      </main>
      <Footer />
    </div>
  );
}