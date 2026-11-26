"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Check, Eye, EyeOff, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { classifyConfirmError, passwordChecks, validateNewPassword, type ResetFailure } from "@/lib/passwordReset";

const inputClass =
  "w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 pr-11 text-[15px] outline-none focus:border-[var(--color-ink)]";

function NewLinkPrompt({ message }: { message: string }) {
  return (
    <div role="alert" className="mt-5">
      <p className="text-[15px] text-[var(--color-ink-soft)]">{message}</p>
      <Link
        href="/forgot-password"
        className="mt-5 inline-block rounded-full bg-[var(--color-ink)] px-6 py-3 text-[14px] font-medium text-[var(--color-paper)]"
      >
        Request a new link
      </Link>
    </div>
  );
}

function ResetForm() {
  const token = useSearchParams().get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failure, setFailure] = useState<ResetFailure | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  if (!token) {
    return <NewLinkPrompt message="This reset link is incomplete. Open the link from your email again, or request a new one." />;
  }

  if (done) {
    return (
      <div role="status" aria-live="polite">
        <p className="mt-3 text-[15px] text-[var(--color-ink-soft)]">
          Your password has been changed and you&apos;ve been signed out everywhere. Log in with the new one.
        </p>
        <Link href="/get-started" className="mt-5 inline-block rounded-full bg-[var(--color-ink)] px-6 py-3 text-[14px] font-medium text-[var(--color-paper)]">
          Log in
        </Link>
      </div>
    );
  }

  if (failure === "invalid_link") {
    return <NewLinkPrompt message="This reset link is invalid, has expired, or was already used. Request a new one to continue." />;
  }

  const checks = passwordChecks(password, confirm);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const problem = validateNewPassword(password, confirm);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    try {
      await api.confirmPasswordReset(token, password);
      setDone(true);
    } catch (err) {
      // Typed passwords stay in place so the user doesn't have to retype them after a transient failure.
      const c = classifyConfirmError(err);
      setFailure(c.kind);
      setError(c.message);
    } finally {
      setBusy(false);
    }
  }

  const Toggle = show ? EyeOff : Eye;

  return (
    <form onSubmit={handleSubmit} className="mt-5 space-y-4" noValidate>
      <div>
        <label htmlFor="new-password" className="mb-1.5 block text-[13.5px] font-medium">
          New password
        </label>
        <div className="relative">
          <input
            id="new-password"
            type={show ? "text" : "password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            aria-describedby="password-rules"
            className={inputClass}
          />
          <button
            type="button"
            onClick={() => setShow((s) => !s)}
            aria-label={show ? "Hide passwords" : "Show passwords"}
            aria-pressed={show}
            className="absolute inset-y-0 right-0 flex w-11 items-center justify-center text-[var(--color-ink-soft)]"
          >
            <Toggle className="h-4 w-4" />
          </button>
        </div>
      </div>
      <div>
        <label htmlFor="confirm-password" className="mb-1.5 block text-[13.5px] font-medium">
          Confirm new password
        </label>
        <input
          id="confirm-password"
          type={show ? "text" : "password"}
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          autoComplete="new-password"
          className={inputClass}
        />
      </div>
      <ul id="password-rules" className="space-y-1 text-[13px]">
        {checks.map((c) => (
          <li key={c.id} className={`flex items-center gap-2 ${c.ok ? "text-[var(--color-ink)]" : "text-[var(--color-ink-soft)]"}`}>
            <Check className={`h-3.5 w-3.5 ${c.ok ? "opacity-100" : "opacity-25"}`} aria-hidden />
            <span>
              {c.label}
              <span className="sr-only">{c.ok ? " — done" : " — not yet"}</span>
            </span>
          </li>
        ))}
      </ul>
      {error && (
        <div role="alert" className="rounded-lg bg-[var(--color-ring-soft)] px-3.5 py-2.5 text-[13.5px] text-[#7a2416]">
          {error}
        </div>
      )}
      <button
        type="submit"
        disabled={busy}
        className="flex w-full items-center justify-center gap-2 rounded-full bg-[var(--color-ink)] py-3 text-[15px] font-medium text-[var(--color-paper)] disabled:opacity-60"
      >
        {busy && <Loader2 className="h-4 w-4 animate-spin" />}
        {busy ? "Saving…" : "Set new password"}
      </button>
    </form>
  );
}

export default function ResetPasswordPage() {
  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <div className="w-full max-w-sm">
        <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Choose a new password</h1>
        <Suspense fallback={null}>
          <ResetForm />
        </Suspense>
      </div>
    </div>
  );
}
