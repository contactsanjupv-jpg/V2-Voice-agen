import Link from "next/link";
import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import { CallDemoCard } from "@/components/CallDemoCard";
import { ArrowRight, Calendar, MessageSquareText, PhoneForwarded } from "lucide-react";

const STEPS = [
  { n: "1", title: "Give us your website", detail: "We read it and learn your services, hours, and FAQs." },
  { n: "2", title: "Review what we found", detail: "Edit anything before it becomes your receptionist's knowledge." },
  { n: "3", title: "Pick a voice and number", detail: "Choose how it sounds, get a phone number in minutes." },
  { n: "4", title: "Turn it on", detail: "Test it yourself first, then activate for real calls." },
];

const CAPABILITIES = [
  { icon: MessageSquareText, title: "Answers like someone who works there", detail: "Trained on your actual services, hours, and policies — not a generic script." },
  { icon: Calendar, title: "Books real appointments", detail: "Checks your actual calendar before offering a time. Never invents availability." },
  { icon: PhoneForwarded, title: "Knows when to hand off", detail: "Transfers to a real person when a caller needs one, on your terms." },
];

export default function Home() {
  return (
    <div className="flex min-h-screen flex-col">
      <Nav />

      <main className="flex-1">
        {/* Hero */}
        <section className="mx-auto max-w-6xl px-6 pb-20 pt-16 md:pb-28 md:pt-24">
          <div className="grid items-center gap-12 md:grid-cols-2 md:gap-8">
            <div>
              <h1 className="font-[family-name:var(--font-display)] text-[42px] font-bold leading-[1.08] tracking-tight md:text-[56px]">
                Every call answered.
                <br />
                Even the ones you&apos;d miss.
              </h1>
              <p className="mt-5 max-w-md text-[17px] leading-relaxed text-[var(--color-ink-soft)]">
                Atla picks up your business phone, answers questions about your business, and books real
                appointments on your calendar — set up from your website in about ten minutes.
              </p>
              <div className="mt-8 flex items-center gap-4">
                <Link
                  href="/get-started"
                  className="group flex items-center gap-2 rounded-full bg-[var(--color-ink)] px-6 py-3.5 text-[15px] font-medium text-[var(--color-paper)] transition-transform hover:scale-[1.02] active:scale-[0.98]"
                >
                  Set up your receptionist
                  <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
                </Link>
                <Link
                  href="/how-it-works"
                  className="text-[15px] font-medium text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]"
                >
                  See how it works
                </Link>
              </div>
            </div>

            <div className="flex justify-center md:justify-end">
              <CallDemoCard />
            </div>
          </div>
        </section>

        {/* Capabilities */}
        <section className="border-y border-[var(--color-line)] bg-[var(--color-paper-raised)]">
          <div className="mx-auto max-w-6xl px-6 py-16">
            <div className="grid gap-10 md:grid-cols-3">
              {CAPABILITIES.map(({ icon: Icon, title, detail }) => (
                <div key={title}>
                  <Icon className="h-6 w-6 text-[var(--color-ring)]" strokeWidth={1.75} />
                  <h3 className="mt-4 font-[family-name:var(--font-display)] text-[18px] font-semibold">{title}</h3>
                  <p className="mt-2 text-[15px] leading-relaxed text-[var(--color-ink-soft)]">{detail}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* How it works preview */}
        <section className="mx-auto max-w-6xl px-6 py-20">
          <div className="flex items-end justify-between">
            <h2 className="font-[family-name:var(--font-display)] text-[28px] font-bold tracking-tight">
              From website to answering calls
            </h2>
            <Link href="/how-it-works" className="hidden text-sm font-medium text-[var(--color-ink-soft)] hover:text-[var(--color-ink)] md:block">
              Full walkthrough →
            </Link>
          </div>

          <div className="mt-10 grid gap-6 md:grid-cols-4">
            {STEPS.map((step) => (
              <div key={step.n} className="border-t border-[var(--color-ink)] pt-4">
                <div className="font-[family-name:var(--font-display)] text-sm text-[var(--color-ink-soft)]">{step.n}</div>
                <h3 className="mt-2 text-[16px] font-semibold">{step.title}</h3>
                <p className="mt-1.5 text-[14px] leading-relaxed text-[var(--color-ink-soft)]">{step.detail}</p>
              </div>
            ))}
          </div>
        </section>

        {/* CTA */}
        <section className="bg-[var(--color-ink)]">
          <div className="mx-auto max-w-6xl px-6 py-20 text-center">
            <h2 className="font-[family-name:var(--font-display)] text-[32px] font-bold tracking-tight text-[var(--color-paper)] md:text-[40px]">
              Stop losing calls to voicemail.
            </h2>
            <p className="mx-auto mt-4 max-w-md text-[16px] text-[#a8abb5]">
              Ten minutes to set up. No developer, no phone system to learn.
            </p>
            <Link
              href="/get-started"
              className="mt-8 inline-flex items-center gap-2 rounded-full bg-[var(--color-ring)] px-7 py-3.5 text-[15px] font-medium text-white transition-transform hover:scale-[1.02] active:scale-[0.98]"
            >
              Get started free
              <ArrowRight className="h-4 w-4" />
            </Link>
          </div>
        </section>
      </main>

      <Footer />
    </div>
  );
}
