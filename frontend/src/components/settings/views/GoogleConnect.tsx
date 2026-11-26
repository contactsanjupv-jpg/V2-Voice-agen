"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { api, describeError } from "@/lib/api";
import { classifyVerifyStartError, googleLinkResult, linkStartOutcome, readLinkVerifyToken } from "@/lib/googleAuth";
import { BTN, INPUT, SettingsSection } from "../ui";

type Status = { configured: boolean; linked: boolean; has_password: boolean };
type Msg = { ok: boolean; text: string } | null;

/**
 * Explicit "Connect Google" for a signed-in user. Hidden when Google sign-in isn't configured on the server.
 * A session alone is never enough: an account with a password re-enters it; an account without one confirms through an
 * emailed one-time link that only works in this browser (finished below when the page opens with ?google_link_verify=...).
 */
export function GoogleConnect() {
  const [status, setStatus] = useState<Status | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [emailSent, setEmailSent] = useState(false);
  // Result of a Google round trip (?google_link=ok|cancelled|...). Read once; unknown values show a generic message.
  // (Nothing renders until the status loads, so reading window here can't cause a hydration mismatch.)
  const [msg, setMsg] = useState<Msg>(() => {
    if (typeof window === "undefined") return null;
    const result = googleLinkResult(new URLSearchParams(window.location.search).get("google_link"));
    return result ? { ok: result.ok, text: result.message } : null;
  });
  const [finishing, setFinishing] = useState<boolean>(() => typeof window !== "undefined" && readLinkVerifyToken(window.location.search) !== null);
  const started = useRef(false);

  useEffect(() => {
    api.googleStatus().then(setStatus).catch(() => setStatus(null));
    const params = new URLSearchParams(window.location.search);
    if (params.has("google_link")) window.history.replaceState(null, "", window.location.pathname + window.location.hash);

    // Finishing from the emailed link. The token is single-use: send it exactly once (also under StrictMode), and take it out
    // of the address bar immediately so it doesn't linger in history or referrers.
    const token = readLinkVerifyToken(window.location.search);
    if (token && !started.current) {
      started.current = true;
      window.history.replaceState(null, "", window.location.pathname + window.location.hash);
      api
        .googleLinkVerifyStart(token)
        .then((res) => {
          const outcome = linkStartOutcome(res);
          if (outcome.kind === "redirect") window.location.assign(outcome.url);
          else throw new Error("unexpected");
        })
        .catch((err) => {
          setMsg({ ok: false, text: classifyVerifyStartError(err) });
          setFinishing(false);
        });
    }
  }, []);

  if (finishing) {
    return (
      <SettingsSection id="google" title="Google sign-in" description="Confirming it's you…">
        <p role="status" aria-live="polite" className="flex items-center gap-2 text-[14px]">
          <Loader2 className="h-4 w-4 animate-spin" /> Taking you to Google…
        </p>
      </SettingsSection>
    );
  }
  if (!status || !status.configured) return null;

  async function connect(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const outcome = linkStartOutcome(await api.googleLinkStart(status!.has_password ? password : null));
      if (outcome.kind === "redirect") {
        window.location.assign(outcome.url);
        return; // keep the spinner until the browser leaves
      }
      if (outcome.kind === "email_sent") setEmailSent(true);
      else setMsg({ ok: false, text: "Something unexpected happened. Nothing was connected." });
    } catch (err) {
      setMsg({ ok: false, text: describeError(err) }); // the password stays in the field
    }
    setBusy(false);
  }

  return (
    <SettingsSection id="google" title="Google sign-in" description="Sign in with your Google account as well as your password.">
      {status.linked ? (
        <p className="text-[14px]">A Google account is connected. You can use “Continue with Google” on the login page.</p>
      ) : emailSent ? (
        <p role="status" aria-live="polite" className="max-w-md text-[14px]">
          We&apos;ve emailed a confirmation link to your account&apos;s address. Open it <span className="font-medium">in this same browser</span>{" "}
          while you&apos;re logged in. It works once and expires in 30 minutes.
        </p>
      ) : (
        <form onSubmit={connect} className="max-w-md space-y-3">
          {status.has_password ? (
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password"
              placeholder="Your Atla password, to confirm it's you" aria-label="Your Atla password" className={INPUT} />
          ) : (
            <p className="text-[13.5px] text-[var(--color-ink-soft)]">
              Your account has no password, so we&apos;ll email you a link to confirm it&apos;s you before connecting Google.
            </p>
          )}
          <button type="submit" disabled={busy || (status.has_password && !password)} className={`${BTN} gap-2`}>
            {busy && <Loader2 className="h-4 w-4 animate-spin" />}
            {status.has_password ? (busy ? "Taking you to Google…" : "Connect Google") : busy ? "Sending…" : "Email me a confirmation link"}
          </button>
        </form>
      )}
      {msg && (
        <p role={msg.ok ? "status" : "alert"} className={`mt-3 text-[13.5px] ${msg.ok ? "text-[var(--color-ink)]" : "text-[#7a2416]"}`}>
          {msg.text}
        </p>
      )}
    </SettingsSection>
  );
}
