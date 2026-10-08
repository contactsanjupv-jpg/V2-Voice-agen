import type { PlanPrice } from "./api";

export function formatDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

const INTERVAL_SHORT: Record<string, string> = { month: "mo", year: "yr", week: "wk", day: "day" };

/** { amount: "$29", cadence: "/mo" } — straight from the billing price, never hardcoded. */
export function formatPrice(price: PlanPrice | null | undefined): { amount: string; cadence: string } | null {
  if (!price) return null;
  try {
    const digits = new Intl.NumberFormat(undefined, { style: "currency", currency: price.currency }).resolvedOptions()
      .maximumFractionDigits ?? 2;
    const value = price.amount_minor / 10 ** digits;
    const whole = Number.isInteger(value);
    const amount = new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: price.currency,
      minimumFractionDigits: whole ? 0 : digits,
      maximumFractionDigits: digits,
    }).format(value);
    const unit = INTERVAL_SHORT[price.interval] ?? price.interval;
    const cadence = price.interval_count === 1 ? `/${unit}` : ` every ${price.interval_count} ${price.interval}s`;
    return { amount, cadence };
  } catch {
    return null;
  }
}

export function formatUsage(seconds: number): string {
  if (seconds < 60) return `${seconds} sec`;
  const minutes = seconds / 60;
  return `${Number.isInteger(minutes) ? minutes : minutes.toFixed(1)} min`;
}