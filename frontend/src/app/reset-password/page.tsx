"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";

function ResetForm() {
  const token = useSearchParams().get("token") ?? "";
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (password.length < 10) {
      setError("Password needs to be at least 10 characters.");
      return;
    }
    setBusy(true);
    try {
      await api.confirmPasswordReset(token, password);
      setDone(true);
    } catch (err) {
      setError(err instanceof ApiError && err.status === 400 ? err.message : "Couldn't reset your password. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div>
        <p className="mt-3 text-[15px] text-[var(--color-ink-soft)]">Your password has been changed. Log in with the new one.</p>
        <Link href="/get-started" className="mt-5 inline-block rounded-full bg-[var(--color-ink)] px-6 py-3 text-[14px] font-medium text-[var(--color-paper)]">
          Log in
        </Link>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="mt-5 space-y-4">
      <input
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        autoComplete="new-password"
        placeholder="New password (at least 10 characters)"
        className="w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[15px] outline-none focus:border-[var(--color-ink)]"
      />
      {error && <div className="rounded-lg bg-[var(--color-ring-soft)] px-3.5 py-2.5 text-[13.5px] text-[#7a2416]">{error}</div>}
      <button
        type="submit"
        disabled={busy || !token}
        className="w-full rounded-full bg-[var(--color-ink)] py-3 text-[15px] font-medium text-[var(--color-paper)] disabled:opacity-60"
      >
        Set new password
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
