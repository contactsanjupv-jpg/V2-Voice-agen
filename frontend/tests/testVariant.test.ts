import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { api } from "../src/lib/api.ts";
import { parseTestVariant } from "../src/lib/testVariant.ts";

/** What the browser actually sends for the test page's query string, exactly as StepTest does it. */
async function requestFor(search: string | null) {
  const calls: { url: string; method: string }[] = [];
  const orig = globalThis.fetch;
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    calls.push({ url, method: init.method ?? "GET" });
    return new Response(JSON.stringify({ call_id: "c", access_token: "t", max_seconds: 60 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
  try {
    const variant = parseTestVariant(new URLSearchParams(search ?? "").get("variant"));
    await api.startTestCall("org1", "agent1", variant);
  } finally {
    globalThis.fetch = orig;
  }
  assert.equal(calls.length, 1);
  return calls[0];
}

test("?variant=closed sends variant=closed to the backend", async () => {
  const c = await requestFor("?variant=closed");
  assert.equal(c.method, "POST");
  assert.match(c.url, /\/api\/v1\/orgs\/org1\/agents\/agent1\/test-call\?variant=closed$/);
});

test("no query parameter sends variant=normal", async () => {
  for (const search of [null, "", "?other=1"]) {
    const c = await requestFor(search);
    assert.match(c.url, /\/agents\/agent1\/test-call\?variant=normal$/, String(search));
  }
});

test("?variant=normal sends variant=normal", async () => {
  assert.match((await requestFor("?variant=normal")).url, /test-call\?variant=normal$/);
});

test("invalid values fall back to normal and never reach the URL", async () => {
  for (const bad of ["?variant=", "?variant=CLOSED", "?variant=closed%20", "?variant=evil", "?variant=closed%26x%3D1", "?variant=..%2Fnormal", "?variant=1"]) {
    const c = await requestFor(bad);
    assert.match(c.url, /test-call\?variant=normal$/, bad);
  }
  assert.equal(parseTestVariant(undefined), "normal");
  assert.equal(parseTestVariant(null), "normal");
});

test("calling api.startTestCall without a variant is the normal receptionist", async () => {
  const calls: string[] = [];
  const orig = globalThis.fetch;
  globalThis.fetch = (async (url: string) => {
    calls.push(url);
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    await api.startTestCall("o", "a");
  } finally {
    globalThis.fetch = orig;
  }
  assert.match(calls[0], /\/test-call\?variant=normal$/);
});

test("the test page reads the query with useSearchParams, validates it, and hands it to startTestCall", () => {
  const t = readFileSync(new URL("../src/components/wizard/StepTest.tsx", import.meta.url), "utf8");
  assert.ok(t.includes("useSearchParams") && t.includes('parseTestVariant(useSearchParams().get("variant"))'));
  assert.ok(t.includes("api.startTestCall(orgId, agentId, variant)"));
  assert.ok(t.includes("<Suspense"), "useSearchParams needs a Suspense boundary for static rendering");
});

test("the SDK mapping is untouched: the session from the backend still goes straight into buildStartCallConfig", () => {
  const t = readFileSync(new URL("../src/components/wizard/StepTest.tsx", import.meta.url), "utf8");
  assert.ok(t.includes("buildStartCallConfig(session)"));
});
