"use client";

import { useCallback, useEffect, useState } from "react";
import { api, PlanOut } from "./api";

/** The plan catalog (names, what's included, Paddle-backed prices) from the backend. */
export function usePlans() {
  const [plans, setPlans] = useState<PlanOut[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    api
      .listPlans()
      .then((p) => {
        if (live) {
          setPlans(p);
          setFailed(false);
        }
      })
      .catch(() => {
        if (live) setFailed(true);
      });
    return () => {
      live = false;
    };
  }, [attempt]);

  const reload = useCallback(() => {
    setFailed(false);
    setPlans(null);
    setAttempt((n) => n + 1);
  }, []);

  return { plans, loading: plans === null && !failed, failed, reload };
}