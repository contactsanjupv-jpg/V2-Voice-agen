"use client";

import { motion } from "framer-motion";

const STEP_LABELS = ["Website", "Review", "Voice", "Behavior", "Test"];

export function WizardShell({
  step,
  children,
}: {
  step: number; // 1-indexed
  children: React.ReactNode;
}) {
  return (
    <div className="mx-auto min-h-screen max-w-2xl px-6 py-12">
      <div className="mb-10">
        <div className="mb-3 flex items-center justify-between">
          <span className="font-[family-name:var(--font-display)] text-lg font-bold">Atla</span>
          <span className="text-[13px] text-[var(--color-ink-soft)]">
            Step {step} of {STEP_LABELS.length}
          </span>
        </div>
        <div className="flex gap-1.5">
          {STEP_LABELS.map((label, i) => (
            <div key={label} className="flex-1">
              <div className="h-1 overflow-hidden rounded-full bg-[var(--color-line)]">
                {i < step && (
                  <motion.div
                    className="h-full bg-[var(--color-ring)]"
                    initial={{ width: 0 }}
                    animate={{ width: "100%" }}
                    transition={{ duration: 0.4, ease: "easeOut" }}
                  />
                )}
              </div>
              <div className="mt-1.5 hidden text-[11px] text-[var(--color-ink-soft)] md:block">{label}</div>
            </div>
          ))}
        </div>
      </div>

      <motion.div
        key={step}
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.35, ease: "easeOut" }}
      >
        {children}
      </motion.div>
    </div>
  );
}

export function WizardActions({
  onBack,
  onNext,
  nextLabel = "Continue",
  nextDisabled = false,
  nextLoading = false,
}: {
  onBack?: () => void;
  onNext: () => void;
  nextLabel?: string;
  nextDisabled?: boolean;
  nextLoading?: boolean;
}) {
  return (
    <div className="mt-8 flex items-center justify-between">
      {onBack ? (
        <button onClick={onBack} className="text-[14px] font-medium text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]">
          Back
        </button>
      ) : (
        <span />
      )}
      <button
        onClick={onNext}
        disabled={nextDisabled || nextLoading}
        className="rounded-full bg-[var(--color-ink)] px-6 py-3 text-[14px] font-medium text-[var(--color-paper)] transition-transform hover:scale-[1.02] active:scale-[0.98] disabled:opacity-50 disabled:hover:scale-100"
      >
        {nextLoading ? "Working…" : nextLabel}
      </button>
    </div>
  );
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="mb-5 rounded-lg bg-[var(--color-ring-soft)] px-4 py-3 text-[13.5px] text-[#7a2416]">{message}</div>
  );
}
