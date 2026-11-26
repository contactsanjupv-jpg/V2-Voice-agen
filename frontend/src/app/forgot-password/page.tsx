"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import {
  RESEND_COOLDOWN_SECONDS,
  classifyRequestError,
  cooldownLabel,
  validateResetEmail,
} from "@/lib/passwordReset";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [cooldown, setCooldown] = useState(0);

  useEffect(() => {
    if (cooldown <= 0) return;
    const t = setTimeout(() => setCooldown((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [cooldown]);

  async function send(address: string) {
    setBusy(true);
    setFormError(null);
    try {
      await api.requestPasswordReset(address);
      setSentTo(address);
      setCooldown(RESEND_COOLDOWN_SECONDS);
    } catch (err) {
      // Never claim an email was sent when the request itself failed. The typed address stays in the field.
      setFormError(classifyRequestError(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const address = email.trim().toLowerCase();
    const problem = validateResetEmail(address);
    setFieldError(problem);
    if (problem) return;
    await send(address);
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <div className="w-full max-w-sm">
        <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Reset your password</h1>
        {sentTo ? (
          <div role="status" aria-live="polite">
            <p className="mt-3 text-[15px] text-[var(--color-ink-soft)]">
              If there&apos;s an Atla account for <span className="font-medium text-[var(--color-ink)]">{sentTo}</span>, we&apos;ve
              sent a link to choose a new password. It expires in an hour and works once. Check your spam folder if it doesn&apos;t
              show up in a few minutes.
            </p>
            {formError && (
              <div role="alert" className="mt-4 rounded-lg bg-[var(--color-ring-soft)] px-3.5 py-2.5 text-[13.5px] text-[#7a2416]">
                {formError}
              </div>
            )}
            <div className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-2 text-[13.5px]">
              <button
                type="button"
                onClick={() => send(sentTo)}
                disabled={busy || cooldown > 0}
                className="flex items-center gap-2 rounded-full border border-[var(--color-line)] px-4 py-2 font-medium disabled:opacity-60"
              >
                {busy && <Loader2 className="h-4 w-4 animate-spin" />}
                {cooldownLabel(cooldown)}
              </button>
              <button
                type="button"
                onClick={() => {
                  setSentTo(null);
                  setFormError(null);
                }}
                className="text-[var(--color-ink-soft)] underline"
              >
                Use a different email
              </button>
            </div>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="mt-5 space-y-4" noValidate>
            <p className="text-[14.5px] text-[var(--color-ink-soft)]">
              Enter the email you signed up with and we&apos;ll send you a link to choose a new password.
            </p>
            <div>
              <label htmlFor="email" className="mb-1.5 block text-[13.5px] font-medium">
                Email
              </label>
              <input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                inputMode="email"
                placeholder="you@business.com"
                aria-invalid={fieldError ? true : undefined}
                aria-describedby={fieldError ? "email-error" : undefined}
                className="w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[15px] outline-none focus:border-[var(--color-ink)]"
              />
              {fieldError && (
                <p id="email-error" className="mt-1.5 text-[13px] text-[#7a2416]">
                  {fieldError}
                </p>
              )}
            </div>
            {formError && (
              <div role="alert" className="rounded-lg bg-[var(--color-ring-soft)] px-3.5 py-2.5 text-[13.5px] text-[#7a2416]">
                {formError}
              </div>
            )}
            <button
              type="submit"
              disabled={busy}
              className="flex w-full items-center justify-center gap-2 rounded-full bg-[var(--color-ink)] py-3 text-[15px] font-medium text-[var(--color-paper)] disabled:opacity-60"
            >
              {busy && <Loader2 className="h-4 w-4 animate-spin" />}
              {busy ? "Sending…" : "Email me a reset link"}
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
