/**
 * In-app notification preferences (pure, no React). One switch per category, for the signed-in person in the active organization.
 * A muted category stops NEW notifications of that category; critical ones are always delivered; history is untouched.
 * State is tagged with its organization and every network result carries the organization it was requested for, so a late
 * response from another organization can never change what is shown (same rule as the notification list).
 */
import type { NotificationCategory } from "./notifications";

export type NotificationPreference = { category: NotificationCategory; enabled: boolean; critical_always_delivered: boolean };

export type PrefsState = {
  orgId: string | null;
  load: "idle" | "loading" | "ready" | "error";
  items: NotificationPreference[];
  saving: Partial<Record<NotificationCategory, boolean>>;
  saveError: NotificationCategory | null;
};

export function initialPrefsFor(orgId: string | null): PrefsState {
  return { orgId, load: "idle", items: [], saving: {}, saveError: null };
}

export type PrefsAction =
  | { type: "switch-org"; orgId: string | null }
  | { type: "load-start" }
  | { type: "loaded"; org: string; items: NotificationPreference[] }
  | { type: "load-failed"; org: string }
  | { type: "toggle"; org: string; category: NotificationCategory; enabled: boolean }  // optimistic
  | { type: "saved"; org: string; item: NotificationPreference }
  | { type: "save-failed"; org: string; category: NotificationCategory; previous: boolean };

export function prefsReducer(s: PrefsState, a: PrefsAction): PrefsState {
  if (a.type === "switch-org") return a.orgId === s.orgId ? s : initialPrefsFor(a.orgId);  // nothing of the previous organization survives
  if ("org" in a && a.org !== s.orgId) return s;  // a late response from another organization: ignored
  switch (a.type) {
    case "load-start":
      return s.load === "ready" ? s : { ...s, load: "loading" };
    case "loaded":
      return { ...s, load: "ready", items: a.items, saveError: null };
    case "load-failed":
      return { ...s, load: s.items.length ? "ready" : "error" };
    case "toggle":
      return {
        ...s,
        items: s.items.map((p) => (p.category === a.category ? { ...p, enabled: a.enabled } : p)),
        saving: { ...s.saving, [a.category]: true },
        saveError: null,
      };
    case "saved":
      return { ...s, items: s.items.map((p) => (p.category === a.item.category ? a.item : p)), saving: { ...s.saving, [a.item.category]: false } };
    case "save-failed":  // put the switch back where the server still has it
      return {
        ...s,
        items: s.items.map((p) => (p.category === a.category ? { ...p, enabled: a.previous } : p)),
        saving: { ...s.saving, [a.category]: false },
        saveError: a.category,
      };
    default:
      return s;
  }
}

export const PREFERENCES_NOTE = "Critical alerts, such as a failed payment or a number that couldn't be set up, are always delivered, even when a category is muted.";

/** Shown under a category that can raise critical alerts. */
export function criticalExplanation(label: string): string {
  return `Critical ${label.toLowerCase()} alerts can't be muted and are always delivered. This switch controls the other ${label.toLowerCase()} notifications.`;
}

/** The switch's accessible name: says what it controls (non-critical only, where the category has critical alerts) and its state. */
export function switchLabel(label: string, enabled: boolean, criticalAlwaysDelivered = false): string {
  const what = criticalAlwaysDelivered ? `${label} non-critical notifications` : `${label} notifications`;
  return `${what}: ${enabled ? "on" : "off"}`;
}
