"use client";

import { CATEGORY_LABEL } from "@/lib/notifications";
import { PREFERENCES_NOTE, criticalExplanation, switchLabel } from "@/lib/notificationPreferences";
import { useNotificationPreferences } from "@/lib/useNotificationPreferences";

/** The preferences view INSIDE the notification drawer (it is not a page, a route or a Settings section). */
export function NotificationPreferences({ orgId, active }: { orgId: string | undefined; active: boolean }) {
  const { state, setEnabled, retry } = useNotificationPreferences(orgId, active);
  return (
    <div className="min-h-0 flex-1 overflow-y-auto" data-testid="notification-preferences">
      <p className="px-5 pb-1 pt-4 text-[13px] text-[var(--color-ink-soft)]">Choose which kinds of notifications you receive. This only affects you.</p>
      {(state.load === "loading" || state.load === "idle") && <p className="px-5 py-8 text-center text-[14px] text-[var(--color-ink-soft)]" role="status">Loading…</p>}
      {state.load === "error" && (
        <div className="px-5 py-8 text-center" role="alert">
          <p className="text-[14px] text-[var(--color-ink-soft)]">We couldn&apos;t load your preferences.</p>
          <button type="button" onClick={() => void retry()} className="mt-3 rounded-full border border-[var(--color-line)] px-4 py-1.5 text-[13px] font-medium">Try again</button>
        </div>
      )}
      {state.load === "ready" && (
        <ul>
          {state.items.map((p) => (
            <li key={p.category} className="flex items-center justify-between gap-3 border-b border-[var(--color-line)] px-5 py-3.5 last:border-b-0">
              <span className="min-w-0">
                <span className="block text-[14.5px] font-medium">{CATEGORY_LABEL[p.category] ?? p.category}</span>
                {p.critical_always_delivered && <span className="block text-[12.5px] text-[var(--color-ink-soft)]" id={`crit-${p.category}`} data-testid={`critical-note-${p.category}`}>{criticalExplanation(CATEGORY_LABEL[p.category] ?? p.category)}</span>}
                {state.saveError === p.category && <span className="block text-[12.5px] text-red-600" role="alert">Couldn&apos;t save. Try again.</span>}
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={p.enabled}
                aria-label={switchLabel(CATEGORY_LABEL[p.category] ?? p.category, p.enabled, p.critical_always_delivered)}
                aria-describedby={p.critical_always_delivered ? `crit-${p.category}` : undefined}
                disabled={!!state.saving[p.category]}
                onClick={() => void setEnabled(p.category, !p.enabled)}
                className={`relative h-6 w-11 shrink-0 rounded-full border border-[var(--color-line)] motion-safe:transition-colors disabled:opacity-60 ${p.enabled ? "bg-[var(--color-ink)]" : "bg-[var(--color-paper)]"}`}
              >
                <span className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow motion-safe:transition-all ${p.enabled ? "left-[22px]" : "left-0.5"}`} />
              </button>
            </li>
          ))}
        </ul>
      )}
      <p className="px-5 py-4 text-[12.5px] text-[var(--color-ink-soft)]">{PREFERENCES_NOTE}</p>
    </div>
  );
}
