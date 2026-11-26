// Pure logic for the forgot-password and reset-password screens (kept out of the components so it can be tested).
import { ApiError } from "./api";

export const MIN_PASSWORD_LENGTH = 10; // mirrors the backend rule (min_length=10)
export const MAX_PASSWORD_LENGTH = 128;
export const RESEND_COOLDOWN_SECONDS = 60;

export function validateResetEmail(email: string): string | null {
  const value = email.trim();
  if (!value) return "Enter your email.";
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return "That doesn't look like a valid email.";
  return null;
}

export interface PasswordCheck {
  id: "length" | "match";
  label: string;
  ok: boolean;
}

export function passwordChecks(password: string, confirm: string): PasswordCheck[] {
  return [
    { id: "length", label: `At least ${MIN_PASSWORD_LENGTH} characters`, ok: password.length >= MIN_PASSWORD_LENGTH },
    { id: "match", label: "Both passwords match", ok: password.length > 0 && password === confirm },
  ];
}

export function validateNewPassword(password: string, confirm: string): string | null {
  if (!password) return "Enter a new password.";
  if (password.length < MIN_PASSWORD_LENGTH) return `Password needs to be at least ${MIN_PASSWORD_LENGTH} characters.`;
  if (password.length > MAX_PASSWORD_LENGTH) return `Password can be at most ${MAX_PASSWORD_LENGTH} characters.`;
  if (password !== confirm) return "The two passwords don't match.";
  return null;
}

export type ResetFailure = "invalid_link" | "rate_limited" | "rejected" | "network";

export interface ClassifiedFailure {
  kind: ResetFailure;
  message: string;
}

/** What went wrong when confirming a reset. `invalid_link` is the only case where the user must request a new link. */
export function classifyConfirmError(err: unknown): ClassifiedFailure {
  if (err instanceof ApiError) {
    if (err.status === 400) return { kind: "invalid_link", message: "This reset link is invalid or has expired." };
    if (err.status === 429) return { kind: "rate_limited", message: "Too many attempts. Please wait a bit and try again." };
    if (err.status === 422) return { kind: "rejected", message: err.message || "That password isn't acceptable." };
    return { kind: "network", message: "Something went wrong on our side. Your password was not changed. Please try again." };
  }
  return { kind: "network", message: "We couldn't reach Atla. Check your connection and try again. Your password was not changed." };
}

/** Requesting a link: the server answers 204 for every address, so the only failures are transport/server ones. */
export function classifyRequestError(err: unknown): string {
  if (err instanceof ApiError && err.status === 422) return "That doesn't look like a valid email.";
  if (err instanceof ApiError) return "Something went wrong on our side. No email was sent. Please try again.";
  return "We couldn't reach Atla. Check your connection and try again. No email was sent.";
}

export function cooldownLabel(secondsLeft: number): string {
  return secondsLeft > 0 ? `Resend in ${secondsLeft}s` : "Resend email";
}
