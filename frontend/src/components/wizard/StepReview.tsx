"use client";

import { useState } from "react";
import { X } from "lucide-react";
import { api, ApiError, StructuredBusinessInfo } from "@/lib/api";
import { WizardActions, ErrorBanner } from "./WizardShell";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1.5 block text-[13px] font-medium text-[var(--color-ink-soft)]">{label}</label>
      {children}
    </div>
  );
}

const inputClass =
  "w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5 text-[14.5px] outline-none focus:border-[var(--color-ink)]";

export function StepReview({
  orgId,
  businessId,
  initial,
  onApproved,
  onBack,
}: {
  orgId: string;
  businessId: string;
  initial: StructuredBusinessInfo;
  onApproved: () => void;
  onBack: () => void;
}) {
  const [name, setName] = useState(initial.business_name || "");
  const [industry, setIndustry] = useState(initial.industry || "");
  const [address, setAddress] = useState(initial.address || "");
  const [phone, setPhone] = useState(initial.phone || "");
  const [description, setDescription] = useState(initial.description || "");
  const [services, setServices] = useState<string[]>(initial.services || []);
  const [newService, setNewService] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function addService() {
    if (newService.trim()) {
      setServices([...services, newService.trim()]);
      setNewService("");
    }
  }

  async function handleNext() {
    setError(null);
    if (!name.trim()) {
      setError("Business name can't be empty.");
      return;
    }
    setLoading(true);
    try {
      await api.approveBusiness(orgId, businessId, {
        name: name.trim(),
        industry: industry.trim() || null,
        address: address.trim() || null,
        description: description.trim() || null,
        hours: initial.hours,
        phone: phone.trim() || null,
        services,
        faqs: initial.faqs || [],
        policies: initial.policies || [],
      });
      onApproved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the server right now.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">
        Here&apos;s what we found
      </h1>
      <p className="mt-2 text-[15px] text-[var(--color-ink-soft)]">
        Nothing is used until you approve it. Fix anything that&apos;s wrong or missing.
      </p>

      <div className="mt-6 space-y-4">
        {error && <ErrorBanner message={error} />}

        <Field label="Business name">
          <input value={name} onChange={(e) => setName(e.target.value)} className={inputClass} />
        </Field>

        <div className="grid grid-cols-2 gap-4">
          <Field label="Industry">
            <input value={industry} onChange={(e) => setIndustry(e.target.value)} className={inputClass} placeholder="e.g. Dental clinic" />
          </Field>
          <Field label="Phone">
            <input value={phone} onChange={(e) => setPhone(e.target.value)} className={inputClass} placeholder="+1…" />
          </Field>
        </div>

        <Field label="Address">
          <input value={address} onChange={(e) => setAddress(e.target.value)} className={inputClass} />
        </Field>

        <Field label="Description">
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            className={`${inputClass} resize-none`}
          />
        </Field>

        <Field label="Services">
          <div className="flex flex-wrap gap-2">
            {services.map((s, i) => (
              <span
                key={i}
                className="flex items-center gap-1.5 rounded-full bg-[var(--color-line)] px-3 py-1.5 text-[13px]"
              >
                {s}
                <button onClick={() => setServices(services.filter((_, idx) => idx !== i))}>
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
          <div className="mt-2 flex gap-2">
            <input
              value={newService}
              onChange={(e) => setNewService(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addService())}
              placeholder="Add a service"
              className={inputClass}
            />
            <button
              onClick={addService}
              className="whitespace-nowrap rounded-lg border border-[var(--color-line)] px-4 text-[14px] font-medium hover:bg-[var(--color-paper-raised)]"
            >
              Add
            </button>
          </div>
        </Field>

        {initial.faqs && initial.faqs.length > 0 && (
          <Field label={`FAQs found (${initial.faqs.length})`}>
            <div className="space-y-2 rounded-lg border border-[var(--color-line)] p-3">
              {initial.faqs.slice(0, 5).map((f, i) => (
                <div key={i} className="text-[13.5px]">
                  <div className="font-medium">{f.question}</div>
                  <div className="text-[var(--color-ink-soft)]">{f.answer}</div>
                </div>
              ))}
            </div>
          </Field>
        )}
      </div>

      <WizardActions onBack={onBack} onNext={handleNext} nextLabel="Looks correct" nextLoading={loading} />
    </div>
  );
}
