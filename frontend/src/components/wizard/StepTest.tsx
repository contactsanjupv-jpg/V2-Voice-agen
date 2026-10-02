"use client";

import { useEffect, useRef, useState } from "react";
import { Mic, PhoneOff, RotateCcw } from "lucide-react";
import { RetellWebClient } from "retell-client-js-sdk";
import { api, ApiError } from "@/lib/api";
import { WizardActions, ErrorBanner } from "./WizardShell";

type Phase = "idle" | "connecting" | "live" | "ended";

// A call counts as a real test once it has actually been live this long.
const MIN_TALK_SECONDS = 5;

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
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [seconds, setSeconds] = useState(0);
  const [maxSeconds, setMaxSeconds] = useState(180);
  const [agentTalking, setAgentTalking] = useState(false);
  const [bestSeconds, setBestSeconds] = useState(0);
  const clientRef = useRef<RetellWebClient | null>(null);
  const secondsRef = useRef(0);
  const maxSecondsRef = useRef(180);

  const tested = bestSeconds >= MIN_TALK_SECONDS;

  // Always hang up if the customer leaves this step mid-call.
  useEffect(() => {
    return () => {
      clientRef.current?.stopCall();
      clientRef.current = null;
    };
  }, []);

  // Call timer + the free-test time cap (configured on the server).
  useEffect(() => {
    if (phase !== "live") return;
    const timer = setInterval(() => {
      secondsRef.current += 1;
      setSeconds(secondsRef.current);
      if (secondsRef.current >= maxSecondsRef.current) clientRef.current?.stopCall();
    }, 1000);
    return () => clearInterval(timer);
  }, [phase]);

  async function handleStart() {
    setError(null);
    secondsRef.current = 0;
    setSeconds(0);
    setAgentTalking(false);
    setPhase("connecting");

    // Ask for the microphone BEFORE starting a server call, so a denied
    // permission never burns one of the customer's free tests.
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((t) => t.stop());
    } catch {
      setPhase("idle");
      setError("We couldn't access your microphone. Allow microphone access in your browser, then try again.");
      return;
    }

    try {
      const session = await api.startTestCall(orgId, agentId);
      maxSecondsRef.current = session.max_seconds;
      setMaxSeconds(session.max_seconds);

      const client = new RetellWebClient();
      clientRef.current = client;
      client.on("call_started", () => setPhase("live"));
      client.on("call_ended", () => {
        setBestSeconds((b) => Math.max(b, secondsRef.current));
        setPhase("ended");
        setAgentTalking(false);
        clientRef.current = null;
      });
      client.on("agent_start_talking", () => setAgentTalking(true));
      client.on("agent_stop_talking", () => setAgentTalking(false));
      client.on("error", () => {
        setError("The call was interrupted. Please try again.");
        setBestSeconds((b) => Math.max(b, secondsRef.current));
        setPhase("ended");
        clientRef.current?.stopCall();
        clientRef.current = null;
      });

      await client.startCall({ accessToken: session.access_token, sampleRate: 24000 });
    } catch (err) {
      clientRef.current = null;
      setPhase("idle");
      setError(err instanceof ApiError ? err.message : "Couldn't start the call. Please try again.");
    }
  }

  function handleEnd() {
    clientRef.current?.stopCall();
  }

  const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
  const ss = String(seconds % 60).padStart(2, "0");

  return (
    <div>
      <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Talk to your receptionist</h1>
      <p className="mt-2 text-[15px] text-[var(--color-ink-soft)]">
        This is a real call to the receptionist you just built. Ask about your hours, services, or prices — and try
        leaving a message.
      </p>

      <div className="mt-6">
        {error && <ErrorBanner message={error} />}

        <div className="flex flex-col items-center rounded-2xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-6 py-10 text-center">
          {phase === "idle" && (
            <>
              <button
                onClick={handleStart}
                className="flex items-center gap-2 rounded-full bg-[var(--color-ink)] px-7 py-3.5 text-[15px] font-medium text-[var(--color-paper)] transition-transform hover:scale-[1.02] active:scale-[0.98]"
              >
                <Mic className="h-4 w-4" /> Start test call
              </button>
              <p className="mt-3 text-[13px] text-[var(--color-ink-soft)]">Your browser will ask for microphone access.</p>
            </>
          )}

          {phase === "connecting" && <p className="text-[15px] text-[var(--color-ink-soft)]">Connecting…</p>}

          {phase === "live" && (
            <>
              <div className="text-[13px] font-medium uppercase tracking-wide text-[var(--color-ok)]">Live</div>
              <div className="mt-1 font-[family-name:var(--font-display)] text-[34px] font-bold tabular-nums">
                {mm}:{ss}
              </div>
              <p className="mt-1 h-5 text-[13.5px] text-[var(--color-ink-soft)]">
                {agentTalking ? "Your receptionist is speaking…" : "Listening — go ahead and speak."}
              </p>
              <button
                onClick={handleEnd}
                className="mt-5 flex items-center gap-2 rounded-full border border-[var(--color-line)] px-6 py-3 text-[14px] font-medium transition-colors hover:bg-[var(--color-paper)]"
              >
                <PhoneOff className="h-4 w-4" /> End call
              </button>
              <p className="mt-3 text-[12.5px] text-[var(--color-ink-soft)]">
                Test calls end automatically after {Math.round(maxSeconds / 60)} minutes.
              </p>
            </>
          )}

          {phase === "ended" && (
            <>
              <p className="text-[15px] font-medium">
                {tested ? "Call ended — that was your real receptionist." : "The call ended before you could try it."}
              </p>
              <button
                onClick={handleStart}
                className="mt-4 flex items-center gap-2 rounded-full border border-[var(--color-line)] px-6 py-3 text-[14px] font-medium transition-colors hover:bg-[var(--color-paper)]"
              >
                <RotateCcw className="h-4 w-4" /> Talk again
              </button>
            </>
          )}
        </div>

        <p className="mt-3 text-[12.5px] text-[var(--color-ink-soft)]">
          Transferring to a person only works on real phone calls, so it can&apos;t be tested here.
        </p>
      </div>

      <WizardActions
        onBack={onBack}
        onNext={onNext}
        nextLabel={tested ? "Go to my dashboard" : "Skip test, go to dashboard"}
        nextDisabled={phase === "live" || phase === "connecting"}
      />
    </div>
  );
}