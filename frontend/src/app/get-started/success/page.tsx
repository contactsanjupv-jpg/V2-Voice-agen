import Link from "next/link";
import { CheckCircle2 } from "lucide-react";
import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";

export default function GetStartedSuccess() {
  return (
    <div className="flex min-h-screen flex-col">
      <Nav />
      <main className="flex flex-1 flex-col items-center justify-center px-6 py-16 text-center">
        <CheckCircle2 className="h-10 w-10 text-[var(--color-ok)]" strokeWidth={1.5} />
        <h1 className="mt-5 font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">
          Account created
        </h1>
        <p className="mt-2 max-w-sm text-[15px] text-[var(--color-ink-soft)]">
          The onboarding wizard — website import, voice, phone number, activation — is being built next.
          You&apos;re signed in; there&apos;s just nowhere to go yet.
        </p>
        <Link
          href="/"
          className="mt-7 rounded-full bg-[var(--color-ink)] px-6 py-3 text-[14px] font-medium text-[var(--color-paper)]"
        >
          Back to home
        </Link>
      </main>
      <Footer />
    </div>
  );
}
