"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { Loader2 } from "lucide-react";

type Mode = "signup" | "login";

function validateEmail(email: string): string | null {
  if (!email) return "Enter your email.";
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return "That doesn't look like a valid email.";
  return null;
}

function validatePassword(password: string): string | null {
  if (!password) return "Enter a password.";
  if (password.length < 10) return "Password needs to be at least 10 characters.";
  return null;
}

export function AuthForm() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("signup");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [orgName, setOrgName] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  function validate(): boolean {
    const errors: Record<string, string> = {};
    const emailErr = validateEmail(email);
    const passwordErr = validatePassword(password);
    if (emailErr) errors.email = emailErr;
    if (passwordErr) errors.password = passwordErr;
    if (mode === "signup" && !orgName.trim()) errors.orgName = "Enter your business name.";
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    if (!validate()) return;

    setSubmitting(true);
    try {
      if (mode === "signup") {
        await api.signup({ email: email.trim().toLowerCase(), password, organization_name: orgName.trim() });
      } else {
        await api.login({ email: email.trim().toLowerCase(), password });
      }
      // Session cookie is now set. Straight into the setup wizard —
      // there's a real destination now, not just a "you're signed in"
      // placeholder.
      router.push("/onboarding");
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 409) {
          setFormError("An account with this email may already exist. Try logging in instead.");
        } else if (err.status === 401) {
          setFormError("That email or password isn't right.");
        } else if (err.status === 429) {
          setFormError("Too many attempts — wait a few minutes and try again.");
        } else {
          setFormError("Something went wrong on our end. Try again in a moment.");
        }
      } else {
        setFormError("Can't reach the server right now. Is the backend running?");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="w-full max-w-sm">
      <div className="mb-7 flex rounded-full border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-1">
        {(["signup", "login"] as Mode[]).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => {
              setMode(m);
              setFieldErrors({});
              setFormError(null);
            }}
            className={`flex-1 rounded-full py-2 text-[14px] font-medium transition-colors ${
              mode === m ? "bg-[var(--color-ink)] text-[var(--color-paper)]" : "text-[var(--color-ink-soft)]"
            }`}
          >
            {m === "signup" ? "Create account" : "Log in"}
          </button>
        ))}
      </div>

      <form onSubmit={handleSubmit} noValidate className="space-y-4">
        {mode === "signup" && (
          <div>
            <label htmlFor="orgName" className="mb-1.5 block text-[13px] font-medium text-[var(--color-ink-soft)]">
              Business name
            </label>
            <input
              id="orgName"
              type="text"
              value={orgName}
              onChange={(e) => setOrgName(e.target.value)}
              autoComplete="organization"
              className="w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[15px] outline-none focus:border-[var(--color-ink)]"
              placeholder="ABC Dental"
            />
            {fieldErrors.orgName && <p className="mt-1.5 text-[13px] text-[var(--color-ring)]">{fieldErrors.orgName}</p>}
          </div>
        )}

        <div>
          <label htmlFor="email" className="mb-1.5 block text-[13px] font-medium text-[var(--color-ink-soft)]">
            Email
          </label>
          <input
            id="email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            className="w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[15px] outline-none focus:border-[var(--color-ink)]"
            placeholder="you@business.com"
          />
          {fieldErrors.email && <p className="mt-1.5 text-[13px] text-[var(--color-ring)]">{fieldErrors.email}</p>}
        </div>

        <div>
          <label htmlFor="password" className="mb-1.5 block text-[13px] font-medium text-[var(--color-ink-soft)]">
            Password
          </label>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "signup" ? "new-password" : "current-password"}
            className="w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[15px] outline-none focus:border-[var(--color-ink)]"
            placeholder="At least 10 characters"
          />
          {fieldErrors.password && <p className="mt-1.5 text-[13px] text-[var(--color-ring)]">{fieldErrors.password}</p>}
        </div>

        {formError && (
          <div className="rounded-lg bg-[var(--color-ring-soft)] px-3.5 py-2.5 text-[13.5px] text-[#7a2416]">
            {formError}
          </div>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="flex w-full items-center justify-center gap-2 rounded-full bg-[var(--color-ink)] py-3 text-[15px] font-medium text-[var(--color-paper)] transition-transform hover:scale-[1.01] active:scale-[0.99] disabled:opacity-60"
        >
          {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
          {mode === "signup" ? "Create account" : "Log in"}
        </button>

        {mode === "login" && (
          <p className="text-center text-[13.5px]">
            <Link href="/forgot-password" className="text-[var(--color-ink-soft)] underline">
              Forgot your password?
            </Link>
          </p>
        )}
      </form>
    </div>
  );
}
