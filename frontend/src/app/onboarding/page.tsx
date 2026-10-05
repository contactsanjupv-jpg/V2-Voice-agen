"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, StructuredBusinessInfo } from "@/lib/api";
import { WizardShell } from "@/components/wizard/WizardShell";
import { StepWebsite } from "@/components/wizard/StepWebsite";
import { StepReview } from "@/components/wizard/StepReview";
import { StepVoice } from "@/components/wizard/StepVoice";
import { StepBehavior } from "@/components/wizard/StepBehavior";
import { StepTest } from "@/components/wizard/StepTest";

export default function OnboardingPage() {
  const router = useRouter();

  const [orgId, setOrgId] = useState<string | null>(null);
  const [orgLoadError, setOrgLoadError] = useState<string | null>(null);
  const [step, setStep] = useState(1);

  const [businessId, setBusinessId] = useState<string | null>(null);
  const [structuredInfo, setStructuredInfo] = useState<StructuredBusinessInfo | null>(null);
  const [voiceId, setVoiceId] = useState<string | null>(null);
  const [agentId, setAgentId] = useState<string | null>(null);
  const [businessName, setBusinessName] = useState<string | null>(null);

  // Progress lives on the server: refresh, close the tab, or come back from
  // another device and we resume exactly where the saved data says you are.
  useEffect(() => {
    api
      .myOrganizations()
      .then(async (orgs) => {
        if (orgs.length === 0) {
          setOrgLoadError("No organization found on your account.");
          return;
        }
        const state = await api.getOnboarding(orgs[0].id);
        if (state.step === "done") {
          router.replace("/dashboard");
          return;
        }
        setBusinessId(state.business_id);
        setBusinessName(state.business_name);
        if (state.structured_info) setStructuredInfo(state.structured_info);
        if (state.agent) {
          setAgentId(state.agent.id);
          setVoiceId(state.agent.voice_id);
        }
        const stepFor = { website: 1, review: 2, voice: 3, behavior: 4, test: 5 } as const;
        const target = state.step === "behavior" && !state.agent?.voice_id ? 3 : stepFor[state.step];
        setStep(target);
        setOrgId(orgs[0].id);
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          router.push("/get-started");
          return;
        }
        setOrgLoadError("Couldn't reach the server. Is the backend running?");
      });
  }, [router]);

  if (orgLoadError) {
    return (
      <div className="flex min-h-screen items-center justify-center px-6 text-center">
        <div>
          <p className="text-[15px] text-[var(--color-ink-soft)]">{orgLoadError}</p>
        </div>
      </div>
    );
  }

  if (!orgId) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-[14px] text-[var(--color-ink-soft)]">Loading…</p>
      </div>
    );
  }

  return (
    <WizardShell step={step}>
      {step === 1 && (
        <StepWebsite
          orgId={orgId}
          onImported={(newBusinessId, info) => {
            setBusinessId(newBusinessId);
            setStructuredInfo(info);
            setStep(2);
          }}
        />
      )}

      {step === 2 && businessId && (
        <StepReview
          orgId={orgId}
          businessId={businessId}
          initial={
            structuredInfo || {
              business_name: "",
              description: null,
              industry: null,
              services: [],
              address: null,
              phone: null,
              hours: null,
              faqs: [],
              policies: [],
            }
          }
          onApproved={() => setStep(3)}
          onBack={() => setStep(1)}
        />
      )}

      {step === 3 && (
        <StepVoice
          onSelected={(id) => {
            setVoiceId(id);
            setStep(4);
          }}
          onBack={() => setStep(2)}
        />
      )}

      {step === 4 && businessId && voiceId && (
        <StepBehavior
          orgId={orgId}
          businessId={businessId}
          businessName={businessName || structuredInfo?.business_name || "Your business"}
          voiceId={voiceId}
          onSaved={(newAgentId) => {
            setAgentId(newAgentId);
            setStep(5);
          }}
          onBack={() => setStep(3)}
        />
      )}

      {step === 5 && agentId && (
        <StepTest orgId={orgId} agentId={agentId} onNext={() => router.push("/dashboard")} onBack={() => setStep(4)} />
      )}
    </WizardShell>
  );
}