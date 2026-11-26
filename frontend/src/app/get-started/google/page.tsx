"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { classifyCompleteError, classifyLinkError, readEmailLinkToken, validateBusinessName, type PendingKind } from "@/lib/googleAuth";

const INPUT =
  "w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[15px] outline-none focus:border-[var(--color-ink)]";
const PRIMARY =
  "flex w-full items-center justify-center gap-2 rounded-full bg-[var(--color-ink)] py-3 text-[15px] font-medium text-[var(--color-paper)] disabled:opacity-60";
const ALERT = "rounded-lg bg-[var(--color-ring-soft)] px-3.5 py-2.5 text-[13.5px] text-[#7a2416]";

function Shell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen items-center justify-center px-6">
      <div className="w-full max-w-sm">
        <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">{title}</h1>
        {children}
      </div>
    </div>
  );
}

function Restart({ message }: { message: string }) {
  return (
    <div role="alert" className="mt-4">
      <p className="text-[15px] text-[var(--color-ink-soft)]">{message}</p>
      <Link href="/get-started" className="mt-5 inline-block rounded-full bg-[var(--color-ink)] px-6 py-3 text-[14px] font-medium text-[var(--color-paper)]">
        Back to log in
      </Link>
    </div>
  );
}

function BusinessName({ email, onExpired }: { email: string; onExpired: () => void }) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const problem = validateBusinessName(name);
    setFieldError(problem);
    setFormError(null);
    if (problem) return;
    setBusy(true);
    try {
      await api.googleComplete(name.trim());
      router.push("/onboarding");
    } catch (err) {
      const c = classifyCompleteError(err);
      if (c.kind === "expired") onExpired();
      else setFormError(c.message);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} noValidate className="mt-3 space-y-4">
      <p className="text-[14.5px] text-[var(--color-ink-soft)]">
        You&apos;re signing up as <span className="font-medium text-[var(--color-ink)]">{email}</span>. What&apos;s your business called?
      </p>
      <div>
        <label htmlFor="orgName" className="mb-1.5 block text-[13.5px] font-medium">Business name</label>
        <input id="orgName" type="text" value={name} onChange={(e) => setName(e.target.value)} autoComplete="organization" autoFocus placeholder="ABC Dental"
          aria-invalid={fieldError ? true : undefined} aria-describedby={fieldError ? "orgName-error" : undefined} className={INPUT} />
        {fieldError && <p id="orgName-error" className="mt-1.5 text-[13px] text-[#7a2416]">{fieldError}</p>}
      </div>
      {formError && <div role="alert" className={ALERT}>{formError}</div>}
      <button type="submit" disabled={busy} className={PRIMARY}>
        {busy && <Loader2 className="h-4 w-4 animate-spin" />}
        {busy ? "Creating your account…" : "Create my account"}
      </button>
    </form>
  );
}

function ConfirmPassword({ email, onExpired }: { email: string; onExpired: () => void }) {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const Toggle = show ? EyeOff : Eye;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!password) return setError("Enter your password.");
    setBusy(true);
    setError(null);
    try {
      await api.googleLinkConfirm(password);
      router.push("/onboarding");
    } catch (err) {
      const c = classifyLinkError(err);
      if (c.kind === "expired") onExpired();
      else setError(c.message); // the password stays in the field
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} noValidate className="mt-3 space-y-4">
      <p className="text-[14.5px] text-[var(--color-ink-soft)]">
        There&apos;s already an Atla account for <span className="font-medium text-[var(--color-ink)]">{email}</span>. To connect your Google
        account to it, enter that account&apos;s password. We never connect accounts just because the email matches.
      </p>
      <div>
        <label htmlFor="link-password" className="mb-1.5 block text-[13.5px] font-medium">Atla password</label>
        <div className="relative">
          <input id="link-password" type={show ? "text" : "password"} value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password" autoFocus className={`${INPUT} pr-11`} aria-describedby={error ? "link-error" : undefined} />
          <button type="button" onClick={() => setShow((v) => !v)} aria-label={show ? "Hide password" : "Show password"} aria-pressed={show}
            className="absolute inset-y-0 right-0 flex w-11 items-center justify-center text-[var(--color-ink-soft)]">
            <Toggle className="h-4 w-4" />
          </button>
        </div>
      </div>
      {error && <div id="link-error" role="alert" className={ALERT}>{error}</div>}
      <button type="submit" disabled={busy} className={PRIMARY}>
        {busy && <Loader2 className="h-4 w-4 animate-spin" />}
        {busy ? "Connecting…" : "Connect Google and log in"}
      </button>
      <p className="text-[13.5px]">
        <Link href="/forgot-password" className="text-[var(--color-ink-soft)] underline">Forgot your password?</Link>
      </p>
    </form>
  );
}

function ConfirmEmail({ email, onExpired }: { email: string; onExpired: () => void }) {
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send() {
    setBusy(true);
    setError(null);
    try {
      await api.googleLinkEmail();
      setSent(true);
    } catch (err) {
      const c = classifyLinkError(err);
      if (c.kind === "expired") onExpired();
      else setError(c.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 space-y-4">
      <p className="text-[14.5px] text-[var(--color-ink-soft)]">
        There&apos;s already an Atla account for <span className="font-medium text-[var(--color-ink)]">{email}</span> that we haven&apos;t been
        able to verify. To connect Google to it, we&apos;ll email a confirmation link to that address.
      </p>
      {sent ? (
        <div role="status" aria-live="polite" className="text-[14.5px] text-[var(--color-ink-soft)]">
          We&apos;ve sent a link to {email}. Open it <span className="font-medium text-[var(--color-ink)]">in this same browser</span>. It expires in 30 minutes.
        </div>
      ) : (
        <button type="button" onClick={send} disabled={busy} className={PRIMARY}>
          {busy && <Loader2 className="h-4 w-4 animate-spin" />}
          {busy ? "Sending…" : "Email me a confirmation link"}
        </button>
      )}
      {error && <div role="alert" className={ALERT}>{error}</div>}
    </div>
  );
}

/** Finishing from the emailed link: needs this browser's Google sign-in (cookie) plus the token in the link. */
function VerifyEmailLink({ token, onExpired }: { token: string; onExpired: (message: string) => void }) {
  const router = useRouter();
  // The emailed token is single-use: send it exactly once, even if this component re-renders or React StrictMode
  // runs the effect twice in development.
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    // The token has been copied into component state: take it out of the address bar and browser history BEFORE the
    // request, so it can't be revisited, bookmarked, shared or leaked through a Referer header.
    window.history.replaceState(null, "", window.location.pathname + window.location.hash);
    api
      .googleLinkVerify(token)
      .then(() => router.push("/onboarding"))
      .catch((err) => onExpired(classifyLinkError(err).message));
  }, [token, router, onExpired]);
  return (
    <p role="status" aria-live="polite" className="mt-4 flex items-center gap-2 text-[15px] text-[var(--color-ink-soft)]">
      <Loader2 className="h-4 w-4 animate-spin" /> Confirming…
    </p>
  );
}

export default function GoogleStep() {
  // Read once (a pure read: safe if React runs the initializer twice). VerifyEmailLink removes it from the URL.
  const [token] = useState<string | null>(() => (typeof window === "undefined" ? null : readEmailLinkToken(window.location.search)));
  const [who, setWho] = useState<{ email: string; name: string; kind: PendingKind } | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    if (token) return; // the emailed-link path doesn't need the summary first
    if (new URLSearchParams(window.location.search).has("verify")) window.history.replaceState(null, "", window.location.pathname + window.location.hash); // malformed leftovers
    let live = true;
    api
      .googlePending()
      .then((p) => live && setWho(p))
      .catch(() => live && setFailure("This sign-in has expired. Please continue with Google again."));
    return () => {
      live = false;
    };
  }, [token]);

  const expire = (message = "This sign-in has expired. Please continue with Google again.") => setFailure(message);

  if (failure) return <Shell title="Let's try that again"><Restart message={failure} /></Shell>;
  if (token) return <Shell title="Connecting Google"><VerifyEmailLink token={token} onExpired={expire} /></Shell>;
  if (!who) return <Shell title="One moment"><div className="mt-6 h-[46px] w-full animate-pulse rounded-lg bg-[var(--color-line)]" aria-label="Loading" /></Shell>;

  switch (who.kind) {
    case "new":
      return <Shell title="One last step"><BusinessName email={who.email} onExpired={() => expire()} /></Shell>;
    case "link_password":
      return <Shell title="Connect your account"><ConfirmPassword email={who.email} onExpired={() => expire()} /></Shell>;
    case "link_email":
      return <Shell title="Confirm it's you"><ConfirmEmail email={who.email} onExpired={() => expire()} /></Shell>;
    default:
      return (
        <Shell title="Use your original sign-in">
          <Restart message={`An Atla account for ${who.email} already exists and signs in with a different Google account. Sign in with that one, or reset the password from the login page.`} />
        </Shell>
      );
  }
}
