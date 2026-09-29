import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import {
  Scissors, Dumbbell, Stethoscope, Wrench, Home as HomeIcon,
  UtensilsCrossed, Car, Scale,
} from "lucide-react";

const INDUSTRIES = [
  { icon: Scissors, name: "Salons & spas", detail: "Booking, service questions, and walk-in availability, handled without pulling a stylist off the floor." },
  { icon: Dumbbell, name: "Gyms & studios", detail: "Class schedules, membership questions, and trial bookings, answered any time someone calls." },
  { icon: Stethoscope, name: "Dental & clinics", detail: "Appointment requests and office-hours questions — kept to what's approved, never medical advice." },
  { icon: Wrench, name: "Home services", detail: "Capture the job details and get a callback scheduled, even for calls that come in mid-job." },
  { icon: HomeIcon, name: "Real estate", detail: "Answer listing questions and book showings without missing a call while you're with a client." },
  { icon: UtensilsCrossed, name: "Restaurants", detail: "Hours, reservations, and menu questions — covered during your busiest hours, not just after." },
  { icon: Car, name: "Automotive", detail: "Service scheduling and basic questions answered, so the shop floor doesn't stop for the phone." },
  { icon: Scale, name: "Professional services", detail: "Intake questions and consultation booking, kept within what you've approved it to say." },
];

export default function Industries() {
  return (
    <div className="flex min-h-screen flex-col">
      <Nav />
      <main className="flex-1">
        <section className="mx-auto max-w-3xl px-6 pb-8 pt-16 md:pt-24">
          <h1 className="font-[family-name:var(--font-display)] text-[40px] font-bold leading-[1.1] tracking-tight md:text-[48px]">
            Built for the business you actually run.
          </h1>
          <p className="mt-5 text-[17px] leading-relaxed text-[var(--color-ink-soft)]">
            Atla isn&apos;t built for one kind of business — it learns yours from your website, whatever that is.
          </p>
        </section>

        <section className="mx-auto max-w-6xl px-6 py-16">
          <div className="grid gap-px overflow-hidden rounded-2xl border border-[var(--color-line)] bg-[var(--color-line)] md:grid-cols-2 lg:grid-cols-4">
            {INDUSTRIES.map(({ icon: Icon, name, detail }) => (
              <div key={name} className="bg-[var(--color-paper-raised)] p-6">
                <Icon className="h-5 w-5 text-[var(--color-ring)]" strokeWidth={1.75} />
                <h3 className="mt-3 text-[15px] font-semibold">{name}</h3>
                <p className="mt-1.5 text-[13.5px] leading-relaxed text-[var(--color-ink-soft)]">{detail}</p>
              </div>
            ))}
          </div>

          <p className="mt-8 text-center text-[14px] text-[var(--color-ink-soft)]">
            Don&apos;t see your kind of business? If you have a website, it works the same way.
          </p>
        </section>
      </main>
      <Footer />
    </div>
  );
}
