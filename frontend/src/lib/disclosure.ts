// Mandatory caller disclosure (Level 8D). Mirrors backend/app/services/disclosure.py, which is the authority: the server
// builds the real opening and rejects deceptive greetings. This only lets owners SEE the real opening and get the same
// error before saving.

export function openingDisclosure(businessName: string): string {
  return `Hi, I'm the AI assistant for ${businessName}. This call is transcribed.`;
}

function earlierDisclosure(businessName: string): string {
  return `Hi, I'm the AI assistant for ${businessName}. This call is transcribed and may also be recorded.`;
}

/** What callers hear first: the disclosure (which cannot be removed), then the owner's greeting. */
export function openingPreview(businessName: string, greeting: string): string {
  const d = openingDisclosure(businessName);
  let g = greeting.trim();
  if (g.startsWith(d)) return g;
  const old = earlierDisclosure(businessName);
  if (g.startsWith(old)) g = g.slice(old.length).trim();
  return g ? `${d} ${g}` : d; // no default greeting: with nothing typed, callers hear only the notice
}

const DECEPTIVE: RegExp[] = [
  /\bi(?:'m| am)\s+(?:a\s+|an\s+)?(?:real|live|actual|living)\s+(?:person|human|human being|agent|receptionist)\b/i,
  /\b(?:i(?:'m| am)|this is|you(?:'re| are)\s+(?:talking|speaking)\s+(?:to|with))\s+(?:a\s+|an\s+)?(?:real\s+|live\s+)?(?:human|person)\b/i,
  /\bnot\s+(?:a|an)\s+(?:robot|bot|ai|machine|computer)\b/i,
  /(?:\bnot|n't)\s+(?:being\s+)?(?:recorded|transcribed)\b/i,
  /\bno\s+(?:recording|transcription)\b/i,
];

export const GREETING_REJECTED =
  "Your greeting can't say or imply that the caller is talking to a human, or that the call isn't transcribed or recorded. " +
  "Atla adds an AI and transcription notice to the start of every call, and it can't be removed.";

/** An error message when the greeting makes a deceptive claim, otherwise null. */
export function greetingProblem(greeting: string): string | null {
  const t = greeting.replace(/[‘’]/g, "'");
  return DECEPTIVE.some((re) => re.test(t)) ? GREETING_REJECTED : null;
}
