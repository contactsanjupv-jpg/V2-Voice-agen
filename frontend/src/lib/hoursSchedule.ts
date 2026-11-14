// Structured opening hours (backend: services/hours_schedule.py). The server is the authority; this mirrors its rules
// so owners see a problem before saving. Times are the business's local wall clock; a window is [start, end).

export const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type Day = (typeof DAYS)[number];
export type Window = [string, string];
export interface HoursSchedule {
  enabled: boolean;
  weekly: Partial<Record<Day, Window[]>>;
  exceptions: Record<string, Window[]>; // YYYY-MM-DD; [] = closed all day
}

export const DAY_LABEL: Record<Day, string> = {
  mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday",
};
export const MAX_WINDOWS_PER_DAY = 4;

export function defaultSchedule(): HoursSchedule {
  const weekly: HoursSchedule["weekly"] = {};
  for (const d of DAYS.slice(0, 5)) weekly[d] = [["09:00", "17:00"]];
  return { enabled: true, weekly, exceptions: {} };
}

function minutes(t: string): number | null {
  const m = /^(\d{2}):(\d{2})$/.exec(t);
  if (!m) return null;
  const h = Number(m[1]), mi = Number(m[2]);
  if (mi > 59 || h > 24 || (h === 24 && mi !== 0)) return null;
  return h * 60 + mi;
}

function windowsProblem(label: string, ws: Window[]): string | null {
  if (ws.length > MAX_WINDOWS_PER_DAY) return `${label}: at most ${MAX_WINDOWS_PER_DAY} windows per day.`;
  const parsed: [number, number][] = [];
  for (const [s, e] of ws) {
    const a = minutes(s), b = minutes(e);
    if (a === null || b === null) return `${label}: enter times like 09:00.`;
    if (a >= b) return `${label}: ${s}-${e} must end after it starts (split overnight hours across two days).`;
    parsed.push([a, b]);
  }
  parsed.sort((x, y) => x[0] - y[0]);
  for (let i = 1; i < parsed.length; i++) if (parsed[i][0] < parsed[i - 1][1]) return `${label}: windows must not overlap.`;
  return null;
}

/** A human-readable problem, or null when the schedule is fine to save. */
export function scheduleProblem(s: HoursSchedule | null): string | null {
  if (!s) return null;
  let any = false;
  for (const d of DAYS) {
    const ws = s.weekly[d] ?? [];
    if (ws.length) any = true;
    const p = windowsProblem(DAY_LABEL[d], ws);
    if (p) return p;
  }
  for (const [date, ws] of Object.entries(s.exceptions)) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || Number.isNaN(Date.parse(`${date}T00:00:00Z`)) || new Date(`${date}T00:00:00Z`).toISOString().slice(0, 10) !== date) return `"${date}" is not a valid date.`;
    const p = windowsProblem(date, ws);
    if (p) return p;
    any = true;
  }
  if (s.enabled && !any) return "Add at least one opening window, or turn the schedule off.";
  return null;
}

export function setDayWindows(s: HoursSchedule, day: Day, ws: Window[]): HoursSchedule {
  const weekly = { ...s.weekly };
  if (ws.length) weekly[day] = ws; else delete weekly[day];
  return { ...s, weekly };
}

export function setException(s: HoursSchedule, date: string, ws: Window[] | null): HoursSchedule {
  const exceptions = { ...s.exceptions };
  if (ws === null) delete exceptions[date]; else exceptions[date] = ws;
  return { ...s, exceptions };
}

/** Shown when opening hours are on but this server won't use them (switch off). Null when nothing is wrong. */
export function serverRoutingNotice(s: HoursSchedule | null | undefined, available: boolean | undefined): string | null {
  if (!s?.enabled || available !== false) return null;
  return "Your hours are saved, but after-hours handling is turned off on this server, so callers still get your normal receptionist. Turn on AFTER_HOURS_ROUTING_ENABLED, restart the backend, then save again.";
}
