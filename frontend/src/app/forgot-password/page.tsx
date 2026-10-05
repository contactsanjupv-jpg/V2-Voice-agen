"use client";

import { useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setBusy(true);
    try {
      await api.requestPasswordReset(email.trim().toLowerCase());
    } catch {
      // The server answers the same way whether or not the account exists; ignore transport errors too.
    }
    setSent(true);
    setBusy(false);
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <div className="w-full max-w-sm">
        <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Reset your password</h1>
        {sent ? (
          <p className="mt-3 text-[15px] text-[var(--color-ink-soft)]">
            If there&apos;s an account for that email, we&apos;ve sent a link to choose a new password. It expires in an hour.
          </p>
        ) : (
          <form onSubmit={handleSubmit} className="mt-5 space-y-4">
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              placeholder="you@business.com"
              className="w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[15px] outline-none focus:border-[var(--color-ink)]"
            />
            <button
              type="submit"
              disabled={busy}
              className="w-full rounded-full bg-[var(--color-ink)] py-3 text-[15px] font-medium text-[var(--color-paper)] disabled:opacity-60"
            >
              Email me a reset link
            </button>
          </form>
        )}
        <p className="mt-6 text-[13.5px]">
          <Link href="/get-started" className="text-[var(--color-ink-soft)] underline">
            Back to log in
          </Link>
        </p>
      </div>
    </div>
  );
}
