// Pure logic for "Continue with Google" (kept out of the components so it can be tested).
import { ApiError } from "./api";

/** The codes the backend can send back as ?google_error=... Anything else is treated as a generic failure. */
export const GOOGLE_ERROR_CODES = ["cancelled", "failed", "expired", "unverified_email", "not_configured", "rate_limited"] as const;
export type GoogleErrorCode = (typeof GOOGLE_ERROR_CODES)[number];

const MESSAGES: Record<GoogleErrorCode, string> = {
  cancelled: "Google sign-in was cancelled. You can try again or use your email and password.",
  failed: "We couldn't sign you in with Google. Please try again, or use your email and password.",
  expired: "That Google sign-in took too long or was already used. Please try again.",
  unverified_email: "Your Google account's email address isn't verified, so we can't use it. Verify it with Google, or sign up with email.",
  not_configured: "Google sign-in isn't set up on this server yet. Use your email and password for now.",
  rate_limited: "Too many sign-in attempts. Wait a few minutes and try again.",
};

/** Message for a ?google_error value, or null when there is no error. Unknown values never echo back into the page. */
export function googleErrorMessage(code: string | string[] | undefined | null): string | null {
  const value = Array.isArray(code) ? code[0] : code;
  if (!value) return null;
  return (GOOGLE_ERROR_CODES as readonly string[]).includes(value) ? MESSAGES[value as GoogleErrorCode] : MESSAGES.failed;
}

/** Where the browser goes. A full-page navigation to the BACKEND, which redirects on to Google. */
export function googleStartUrl(apiBase: string): string {
  return `${apiBase.replace(/\/+$/, "")}/api/v1/auth/google/start`;
}

export function validateBusinessName(name: string): string | null {
  const value = name.trim();
  if (!value) return "Enter your business name.";
  if (value.length > 255) return "Business name can be at most 255 characters.";
  return null;
}

export type CompleteFailure = "expired" | "invalid" | "rate_limited" | "network";

export function classifyCompleteError(err: unknown): { kind: CompleteFailure; message: string } {
  if (err instanceof ApiError) {
    if (err.status === 400 || err.status === 404)
      return { kind: "expired", message: "This sign-up has expired. Please continue with Google again." };
    if (err.status === 422) return { kind: "invalid", message: "Enter a valid business name." };
    if (err.status === 429) return { kind: "rate_limited", message: "Too many attempts. Please wait a bit and try again." };
    return { kind: "network", message: "Something went wrong on our side. Your account was not created. Please try again." };
  }
  return { kind: "network", message: "We couldn't reach Atla. Check your connection and try again. Your account was not created." };
}

export type PendingKind = "new" | "link_password" | "link_email" | "blocked";

export type LinkFailure = "wrong_password" | "rate_limited" | "expired" | "conflict" | "network";

/** Failures while proving ownership of an existing Atla account. Only "expired" sends the person back to the start. */
export function classifyLinkError(err: unknown): { kind: LinkFailure; message: string } {
  if (err instanceof ApiError) {
    if (err.status === 401) return { kind: "wrong_password", message: "That password isn't correct." };
    if (err.status === 429)
      return { kind: "rate_limited", message: "Too many attempts. Please wait a few minutes, or reset your password with “Forgot your password?”." };
    if (err.status === 400 || err.status === 404)
      return { kind: "expired", message: "This link or sign-in has expired, or was started in a different browser. Please continue with Google again." };
    if (err.status === 409) return { kind: "conflict", message: err.message || "This Google account can't be connected to that Atla account." };
    return { kind: "network", message: "Something went wrong on our side. Nothing was connected. Please try again." };
  }
  return { kind: "network", message: "We couldn't reach Atla. Check your connection and try again. Nothing was connected." };
}

/** Result of an explicit "Connect Google" from Settings, as ?google_link=<code>. */
const LINK_RESULTS: Record<string, { ok: boolean; message: string }> = {
  ok: { ok: true, message: "Google is connected. You can now sign in with it." },
  cancelled: { ok: false, message: "Connecting Google was cancelled. Nothing changed." },
  failed: { ok: false, message: "We couldn't connect Google. Nothing changed. Please try again." },
  expired: { ok: false, message: "That attempt took too long or was already used. Please try again." },
  session_changed: { ok: false, message: "You were signed in as someone else when Google came back, so nothing was connected." },
  already_linked: { ok: false, message: "That Google account is already connected to a different Atla account." },
  already_connected: { ok: false, message: "A different Google account is already connected to your Atla account. Nothing changed." },
  unverified_email: { ok: false, message: "That Google account's email isn't verified, so it can't be used." },
  rate_limited: { ok: false, message: "Too many attempts. Wait a few minutes and try again." },
  not_configured: { ok: false, message: "Google sign-in isn't set up on this server yet." },
};

export function googleLinkResult(code: string | null | undefined): { ok: boolean; message: string } | null {
  if (!code) return null;
  return LINK_RESULTS[code] ?? LINK_RESULTS.failed; // unknown values never echo into the page
}

/** What to do with the answer to "Connect Google": go to Google, or tell the person to check their email. Anything else is a bug. */
export function linkStartOutcome(res: { url?: string; verification?: string }): { kind: "redirect"; url: string } | { kind: "email_sent" } | { kind: "unexpected" } {
  if (typeof res.url === "string" && res.url.startsWith("https://accounts.google.com/")) return { kind: "redirect", url: res.url };
  if (res.verification === "email_sent") return { kind: "email_sent" };
  return { kind: "unexpected" }; // never navigate anywhere the server didn't clearly say is Google
}

/** The one-time token in an emailed link (?google_link_verify=...). The page removes it from the address bar right away. */
export function readLinkVerifyToken(search: string): string | null {
  const t = new URLSearchParams(search).get("google_link_verify");
  return t && /^[\w-]{10,200}$/.test(t) ? t : null;
}

/** The one-time token in the emailed "finish connecting" link (`?verify=`). Anything that isn't token-shaped is ignored. */
export function readEmailLinkToken(search: string): string | null {
  const t = new URLSearchParams(search).get("verify");
  return t && /^[\w-]{10,200}$/.test(t) ? t : null;
}

/** Failures of the emailed-link step. A 400 covers wrong browser, used, expired, or a changed session. */
export function classifyVerifyStartError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 400)
      return "This link is invalid, has expired, was already used, or was opened in a different browser or session than the one where you clicked Connect Google. Start again from here.";
    if (err.status === 401) return "Please log in, then open the link again in the same browser.";
    if (err.status === 409) return err.message || "Google can't be connected to this account right now.";
    if (err.status === 429) return "Too many attempts. Please wait a few minutes and try again.";
    return "Something went wrong on our side. Nothing was connected. Please try again.";
  }
  return "We couldn't reach Atla. Check your connection and try again. Nothing was connected.";
}
