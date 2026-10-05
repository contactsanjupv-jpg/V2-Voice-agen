"use client";

import { useEffect, useRef, useState } from "react";
import { CheckCircle2 } from "lucide-react";
import { CheckoutEventNames, initializePaddle, type Paddle } from "@paddle/paddle-js";
import { api, ApiError, SubscriptionOut } from "@/lib/api";
import { WizardActions, ErrorBanner } from "./WizardShell";

// Display prices only — the real charge comes from the Paddle price IDs
// configured on the backend. Keep these in sync with your Paddle prices.
const PLANS = [
  { id: "starter", name: "Starter", price: "$79" },
  { id: "growth", name: "Growth", price: "$199" },
] as const;

const ACTIVE_STATUSES = ["active", "trialing"];
const PADDLE_TOKEN = process.env.NEXT_PUBLIC_PADDLE_CLIENT_TOKEN;
const PADDLE_ENV = process.env.NEXT_PUBLIC_PADDLE_ENV === "live" ? "production" : "sandbox";

export function StepPlan({
  orgId,
  onSubscribed,
  onBack,
}: {
  orgId: string;
  onSubscribed: () => void;
  onBack: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [subscription, setSubscription] = useState<SubscriptionOut | null>(null);
  const [busyPlan, setBusyPlan] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const paddleRef = useRef<Promise<Paddle | undefined> | null>(null);

  const isActive = subscription !== null && ACTIVE_STATUSES.includes(subscription.status);

  useEffect(() => {
    api
      .getSubscription(orgId)
      .then((sub) => setSubscription(sub))
      .catch(() => setError("Couldn't check your plan. Is the backend running?"))
      .finally(() => setLoading(false));
  }, [orgId]);

  // After Paddle reports payment, the subscription row is written by the
  // webhook (server-to-server), so poll until it shows up as active.
  useEffect(() => {
    if (!waiting) return;
    let cancelled = false;
    (async () => {
      for (let i = 0; i < 30 && !cancelled; i++) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        try {
          const sub = await api.getSubscription(orgId);
          if (cancelled) return;
          if (sub && ACTIVE_STATUSES.includes(sub.status)) {
            setSubscription(sub);
            setWaiting(false);
            return;
          }
        } catch {
          // transient error — keep polling
        }
      }
      if (!cancelled) {
        setWaiting(false);
        setError("Payment received, but confirmation is taking longer than usual. Wait a minute, then check again.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [waiting, orgId]);

  function getPaddle() {
    if (!paddleRef.current) {
      paddleRef.current = initializePaddle({
        environment: PADDLE_ENV,
        token: PADDLE_TOKEN!,
        eventCallback: (event) => {
          if (event.name === CheckoutEventNames.CHECKOUT_COMPLETED) {
            setBusyPlan(null);
            setWaiting(true);
          } else if (event.name === CheckoutEventNames.CHECKOUT_CLOSED) {
            setBusyPlan(null);
          }
        },
      });
    }
    return paddleRef.current;
  }

  async function handleChoose(plan: "starter" | "growth") {
    setError(null);
    if (!PADDLE_TOKEN) {
      setError("Payments aren't configured yet.");
      return;
    }
    setBusyPlan(plan);
    try {
      const { checkout_url } = await api.createCheckoutSession(orgId, plan);
      const transactionId = checkout_url.match(/txn_[a-z0-9]+/i)?.[0];
      const paddle = await getPaddle();
      if (!transactionId || !paddle) throw new Error("checkout unavailable");
      paddle.Checkout.open({ transactionId });
    } catch (err) {
      setBusyPlan(null);
      setError(
        err instanceof ApiError && (err.status === 403 || err.status === 409)
          ? err.status === 403
            ? "Only the account owner can choose a plan."
            : err.message
          : "Couldn't start checkout. Please try again."
      );
    }
  }

  async function handleCheckAgain() {
    setError(null);
    try {
      const sub = await api.getSubscription(orgId);
      setSubscription(sub);
      if (!sub || !ACTIVE_STATUSES.includes(sub.status)) setError("Not confirmed yet — try again in a minute.");
    } catch {
      setError("Couldn't check your plan right now.");
    }
  }

  return (
    <div>
      <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Choose your plan</h1>
      <p className="mt-2 text-[15px] text-[var(--color-ink-soft)]">
        A plan is needed before we can set up your phone number and put your receptionist live.
      </p>

      <div className="mt-6">
        {error && <ErrorBanner message={error} />}

        {loading ? (
          <p className="text-[14px] text-[var(--color-ink-soft)]">Checking your plan…</p>
        ) : isActive ? (
          <div className="flex items-center gap-3 rounded-xl border border-[var(--color-ok)]/30 bg-[var(--color-ok)]/10 px-4 py-3.5">
            <CheckCircle2 className="h-5 w-5 flex-shrink-0 text-[var(--color-ok)]" />
            <div className="text-[15px] font-medium">
              You&apos;re on the {PLANS.find((p) => p.id === subscription?.plan_id)?.name ?? subscription?.plan_id} plan
            </div>
          </div>
        ) : waiting ? (
          <p className="text-[14px] text-[var(--color-ink-soft)]">Confirming your payment…</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {PLANS.map((plan) => (
              <button
                key={plan.id}
                onClick={() => handleChoose(plan.id)}
                disabled={busyPlan !== null}
                className="rounded-xl border border-[var(--color-line)] bg-[var(--color-paper-raised)] p-5 text-left transition-colors hover:border-[var(--color-ink)] disabled:opacity-60"
              >
                <div className="text-[15px] font-medium">{plan.name}</div>
                <div className="mt-1 font-[family-name:var(--font-display)] text-[28px] font-bold">
                  {plan.price}
                  <span className="text-[14px] font-normal text-[var(--color-ink-soft)]">/mo</span>
                </div>
                <div className="mt-3 text-[13.5px] font-medium text-[var(--color-ink-soft)]">
                  {busyPlan === plan.id ? "Opening checkout…" : `Choose ${plan.name}`}
                </div>
              </button>
            ))}
          </div>
        )}

        {!loading && !isActive && !waiting && error && (
          <button onClick={handleCheckAgain} className="mt-4 text-[13.5px] font-medium underline">
            Already paid? Check again
          </button>
        )}
      </div>

      <WizardActions onBack={onBack} onNext={onSubscribed} nextDisabled={!isActive} />
    </div>
  );
}