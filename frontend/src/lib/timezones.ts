/**
 * Business timezone choices. The server stores an IANA name ("America/Chicago") and validates it; this list is only
 * what the pickers offer. Atla's first customers are US home-service businesses, so the US zones come first.
 */
export const US_TIMEZONES: { value: string; label: string }[] = [
  { value: "America/New_York", label: "Eastern (New York)" },
  { value: "America/Chicago", label: "Central (Chicago)" },
  { value: "America/Denver", label: "Mountain (Denver)" },
  { value: "America/Phoenix", label: "Mountain, no daylight saving (Phoenix)" },
  { value: "America/Los_Angeles", label: "Pacific (Los Angeles)" },
  { value: "America/Anchorage", label: "Alaska (Anchorage)" },
  { value: "Pacific/Honolulu", label: "Hawaii (Honolulu)" },
];

/** The device's IANA timezone, or "" when the browser can't say. */
export function detectTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

/**
 * Options for a timezone <select>. A saved or detected zone that isn't one of the US zones (for example
 * America/Puerto_Rico) is still offered, so the picker never silently replaces a value with something else.
 */
export function timezoneOptions(current: string, detected = ""): { value: string; label: string }[] {
  const out = [...US_TIMEZONES];
  for (const z of [current, detected]) {
    if (z && !out.some((o) => o.value === z)) out.push({ value: z, label: z.replace(/_/g, " ") });
  }
  return out;
}

/** What a new business should start with: this device's zone when it is a real zone, otherwise nothing. */
export function defaultTimezone(detected: string): string {
  return detected.includes("/") ? detected : "";
}
