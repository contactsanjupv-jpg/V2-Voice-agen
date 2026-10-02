"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { WizardActions, ErrorBanner } from "./WizardShell";

const TASK_OPTIONS: { key: string; label: string }[] = [
  { key: "answer_questions", label: "Answer questions" },
  { key: "capture_leads", label: "Capture leads" },
  { key: "transfer_calls", label: "Transfer calls to a person" },
  { key: "take_messages", label: "Take messages" },
];

const PERSONALITIES = ["Friendly", "Professional", "Concise"];

const inputClass =
  "w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[14.5px] outline-none focus:border-[var(--color-ink)]";

export function StepBehavior({
  orgId,
  businessId,
  businessName,
  voiceId,
  onSaved,
  onBack,
}: {
  orgId: string;
  businessId: string;
  businessName: string;
  voiceId: string;
  onSaved: (agentId: string) => void;
  onBack: () => void;
}) {
  const [name, setName] = useState(`${businessName} Receptionist`);
  const [greeting, setGreeting] = useState(`Hi, thanks for calling ${businessName}, how can I help?`);
  const [personality, setPersonality] = useState("Professional");
  const [tasks, setTasks] = useState<Record<string, boolean>>({
  answer_questions: true,
  capture_leads: true,
  transfer_calls: false,
  take_messages: true,
});
  const [transferNumber, setTransferNumber] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function toggleTask(key: string) {
    setTasks((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  async function handleNext() {
    setError(null);
    setLoading(true);
    try {
      const agent = await api.upsertAgent(orgId, businessId, {
        name,
        greeting: greeting || null,
        personality: personality.toLowerCase(),
        language: "en-US",
        voice_id: voiceId,
        tasks,
        transfer_number: tasks.transfer_calls ? transferNumber.trim() || null : null,
        business_hours: null,
        after_hours_behavior: null,
      });
      onSaved(agent.id);
    } catch (err) {
  setError(err instanceof ApiError ? err.message : "Can't reach the server right now.");
  } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">How should it act?</h1>
      <p className="mt-2 text-[15px] text-[var(--color-ink-soft)]">
  Set the basics — no scripting or prompts to write. It already knows what we learned from your business.
</p>

      <div className="mt-6 space-y-4">
        {error && <ErrorBanner message={error} />}

        <div>
          <label className="mb-1.5 block text-[13px] font-medium text-[var(--color-ink-soft)]">Receptionist name</label>
          <input value={name} onChange={(e) => setName(e.target.value)} className={inputClass} />
        </div>

        <div>
          <label className="mb-1.5 block text-[13px] font-medium text-[var(--color-ink-soft)]">Greeting</label>
          <textarea value={greeting} onChange={(e) => setGreeting(e.target.value)} rows={2} className={`${inputClass} resize-none`} />
        </div>

        <div>
          <label className="mb-1.5 block text-[13px] font-medium text-[var(--color-ink-soft)]">Personality</label>
          <div className="flex gap-2">
            {PERSONALITIES.map((p) => (
              <button
                key={p}
                onClick={() => setPersonality(p)}
                className={`rounded-full border px-4 py-2 text-[13.5px] font-medium transition-colors ${
                  personality === p
                    ? "border-[var(--color-ink)] bg-[var(--color-ink)] text-[var(--color-paper)]"
                    : "border-[var(--color-line)] hover:bg-[var(--color-paper-raised)]"
                }`}
              >
                {p}
              </button>
            ))}
          </div>
        </div>

        <div>
          <label className="mb-1.5 block text-[13px] font-medium text-[var(--color-ink-soft)]">What should it do?</label>
          <div className="space-y-2">
            {TASK_OPTIONS.map((t) => (
              <label
                key={t.key}
                className="flex cursor-pointer items-center gap-3 rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-4 py-3"
              >
                <input
                  type="checkbox"
                  checked={!!tasks[t.key]}
                  onChange={() => toggleTask(t.key)}
                  className="h-4 w-4 accent-[var(--color-ink)]"
                />
                <span className="text-[14.5px]">{t.label}</span>
              </label>
            ))}
          </div>
        </div>

        {tasks.transfer_calls && (
          <div>
            <label className="mb-1.5 block text-[13px] font-medium text-[var(--color-ink-soft)]">Transfer number</label>
            <input
              value={transferNumber}
              onChange={(e) => setTransferNumber(e.target.value)}
              placeholder="+14155551234"
              className={inputClass}
            />
          </div>
        )}
      </div>

      <WizardActions onBack={onBack} onNext={handleNext} nextLabel="Save receptionist" nextLoading={loading} />
    </div>
  );
}
