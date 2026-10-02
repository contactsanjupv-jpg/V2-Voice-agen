"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useOrganization } from "@/lib/useOrganization";
import { api } from "@/lib/api";
import { StepTest } from "@/components/wizard/StepTest";
import { DashboardShell, LoadingScreen, ErrorScreen } from "@/components/dashboard/DashboardShell";

export default function TestPage() {
  const router = useRouter();
  const { org, loading, error } = useOrganization();
  const [agentId, setAgentId] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);

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
        {org && agentId && (
          <StepTest orgId={org.id} agentId={agentId} onNext={() => router.push("/dashboard")} onBack={() => router.push("/dashboard")} />
        )}
      </div>
    </DashboardShell>
  );
}