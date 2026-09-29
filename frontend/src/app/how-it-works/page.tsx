import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import { Check } from "lucide-react";

const STAGES = [
  {
    n: "01",
    title: "Give us your website",
    body: "Enter your website address. We read it the way a person would — services, hours, address, FAQs, policies — and never anything you haven't published publicly.",
  },
  {
    n: "02",
    title: "Review what we found",
    body: "Nothing goes live until you've seen it. Edit, correct, or add anything before it becomes part of your receptionist's knowledge.",
    checks: ["Business info", "Services", "Hours", "FAQs"],
  },
  {
    n: "03",
    title: "Pick a voice",
    body: "Browse real voices, preview them, and choose the one that sounds like your business.",
  },
  {
    n: "04",
    title: "Get a phone number",
    body: "Get a new number in minutes, or we can help route your existing one.",
  },
  {
    n: "05",
    title: "Set how it behaves",
    body: "Choose what it should do: answer questions, capture leads, book appointments, transfer calls, take messages. Set a transfer number and your hours.",
  },
  {
    n: "06",
    title: "Connect your calendar",
    body: "Link Google Calendar so it only ever offers times that are actually open — it checks in real time, every call.",
  },
  {
    n: "07",
    title: "Test it yourself",
    body: "Call it, or test right in your browser, before a single real customer ever reaches it.",
  },
  {
    n: "08",
    title: "Turn it on",
    body: "Activate, and it starts answering. Watch calls, leads, and booked appointments land on your dashboard as they happen.",
  },
];

export default function HowItWorks() {
  return (
    <div className="flex min-h-screen flex-col">
      <Nav />
      <main className="flex-1">
        <section className="mx-auto max-w-3xl px-6 pb-8 pt-16 md:pt-24">
          <h1 className="font-[family-name:var(--font-display)] text-[40px] font-bold leading-[1.1] tracking-tight md:text-[48px]">
            From your website to answering the phone.
          </h1>
          <p className="mt-5 text-[17px] leading-relaxed text-[var(--color-ink-soft)]">
            Eight steps, about ten minutes of your time. No developer, no phone system to configure.
          </p>
        </section>

        <section className="mx-auto max-w-3xl px-6 py-16">
          <div className="space-y-14">
            {STAGES.map((stage) => (
              <div key={stage.n} className="grid grid-cols-[56px_1fr] gap-6 md:grid-cols-[72px_1fr]">
                <div className="font-[family-name:var(--font-display)] text-[28px] font-bold text-[var(--color-line)] md:text-[34px]">
                  {stage.n}
                </div>
                <div className="border-t border-[var(--color-line)] pt-1">
                  <h2 className="text-[19px] font-semibold">{stage.title}</h2>
                  <p className="mt-2 text-[15px] leading-relaxed text-[var(--color-ink-soft)]">{stage.body}</p>
                  {stage.checks && (
                    <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-1.5">
                      {stage.checks.map((c) => (
                        <li key={c} className="flex items-center gap-1.5 text-[13px] text-[var(--color-ink-soft)]">
                          <Check className="h-3.5 w-3.5 text-[var(--color-ok)]" />
                          {c}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>
      </main>
      <Footer />
    </div>
  );
}
