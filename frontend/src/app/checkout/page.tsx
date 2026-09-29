"use client";

import { useEffect } from "react";
import { initializePaddle } from "@paddle/paddle-js";

/**
 * Paddle's "default payment link" page. Paddle requires one, and checkout
 * URLs it generates point here with ?_ptxn=txn_... — initializing Paddle.js
 * on this page opens the checkout for that transaction automatically.
 * The normal flow opens checkout inside the onboarding wizard instead.
 */
export default function CheckoutPage() {
  useEffect(() => {
    const token = process.env.NEXT_PUBLIC_PADDLE_CLIENT_TOKEN;
    if (!token) return;
    initializePaddle({
      environment: process.env.NEXT_PUBLIC_PADDLE_ENV === "live" ? "production" : "sandbox",
      token,
    });
  }, []);

  return (
    <div className="flex min-h-screen items-center justify-center px-6 text-center">
      <p className="text-[14px] text-[var(--color-ink-soft)]">Loading checkout…</p>
    </div>
  );
}