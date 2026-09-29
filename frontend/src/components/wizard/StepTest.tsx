"use client";

import { useState } from "react";
import { PhoneCall } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { WizardActions, ErrorBanner } from "./WizardShell";

export function StepTest({
  orgId,
  agentId,
  onNext,
  onBack,
}: {
  orgId: string;
  agentId: string;
  onNext: () => void;
  onBack: () => void;
}) {
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState(false);

  async function handleStartTest() {
    setError(null);
    setStarting(true);
    try {
      // Real call session against Retell — access_token would be handed to
      // a WebRTC client (Retell's client SDK) to actually join the call.
      // That audio-in-browser piece isn't wired up yet; this confirms the
      // session itself starts for real.
      await api.startTestCall(orgId, agentId);
      setStarted(true);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${err.message} — test calls need a real Retell API key configured on the backend.`
          : "Can't reach the server right now."
      );
    } finally {
      setStarting(false);
    }
  }

  return (
    <div>
      <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Test it yourself</h1>
      <p className="mt-2 text-[15px] text-[var(--color-ink-soft)]">
        Try a call before any real customer reaches your receptionist.
      </p>

      <div className="mt-8 flex flex-col items-center rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-6 py-12 text-center">
        {error && <ErrorBanner message={error} />}

        {!started ? (
          <>
            <button
              onClick={handleStartTest}
              disabled={starting}
              className="flex h-16 w-16 items-center justify-center rounded-full bg-[var(--color-ring)] text-white transition-transform hover:scale-105 disabled:opacity-60"
            >
              <PhoneCall className="h-6 w-6" />
            </button>
            <p className="mt-4 text-[14px] text-[var(--color-ink-soft)]">
              {starting ? "Starting test call…" : "Tap to start a test call"}
            </p>
          </>
        ) : (
          <>
            <div className="flex h-16 w-16 items-center justify-center rounded-full bg-[var(--color-ok)]/15">
              <PhoneCall className="h-6 w-6 text-[var(--color-ok)]" />
            </div>
            <p className="mt-4 text-[14.5px] font-medium">Test call session started</p>
            <p className="mt-1 text-[13px] text-[var(--color-ink-soft)]">
              In-browser audio isn&apos;t wired up in this build yet — the call session itself is real.
            </p>
          </>
        )}
      </div>

      <WizardActions onBack={onBack} onNext={onNext} nextLabel="It sounds good, continue" />
    </div>
  );
}
