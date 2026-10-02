"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useOrganization } from "@/lib/useOrganization";
import { api } from "@/lib/api";
import { StepPlan } from "@/components/wizard/StepPlan";
import { StepPhoneNumber } from "@/components/wizard/StepPhoneNumber";
import { StepActivate } from "@/components/wizard/StepActivate";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";

type Stage = "plan" | "number" | "activate";

/**
 * Payment lives HERE, after the customer has built, heard, and seen their
 * receptionist — not in onboarding. Nothing on this page spends provider
 * money until the backend confirms an active subscription.
 */
export default function GoLivePage() {
  const router = useRouter();
  const { org, loading, error } = useOrganization();
  const [agentId, setAgentId] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [stage, setStage] = useState<Stage>("plan");
  const [phoneNumberId, setPhoneNumberId] = useState<string | null>(null);

  useEffect(() => {
    if (!org) return;
    api
      .listAgents(org.id)
      .then((agents) => {
        if (agents.length === 0) router.replace("/onboarding");
        else setAgentId(agents[0].id);
      })
      .finally(() => setChecking(false));
  }, [org, router]);

  if (loading || checking) return <LoadingScreen />;
  if (error) return <ErrorScreen message={error} />;

  return (
    <DashboardShell org={org}>
      <div className="max-w-xl">
        {org && stage === "plan" && (
          <StepPlan orgId={org.id} onSubscribed={() => setStage("number")} onBack={() => router.push("/dashboard")} />
        )}
        {org && stage === "number" && (
          <StepPhoneNumber
            orgId={org.id}
            onPurchased={(id) => {
              setPhoneNumberId(id);
              setStage("activate");
            }}
            onBack={() => setStage("plan")}
          />
        )}
        {org && stage === "activate" && agentId && phoneNumberId && (
          <StepActivate orgId={org.id} agentId={agentId} phoneNumberId={phoneNumberId} onBack={() => setStage("number")} />
        )}
      </div>
    </DashboardShell>
  );
}