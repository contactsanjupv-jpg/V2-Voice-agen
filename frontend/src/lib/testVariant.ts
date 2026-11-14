// Which receptionist a browser test call talks to. `normal` is the regular receptionist; `closed` is the after-hours
// twin (Level 8C, no transfer tool). Chosen with `?variant=closed` on the test page. Anything else means normal, so a
// typo can never silently pick the wrong agent in a surprising way or break the call.

export type TestVariant = "normal" | "closed";

export function parseTestVariant(raw: string | null | undefined): TestVariant {
  return raw === "closed" ? "closed" : "normal";
}
