import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { buildStartCallConfig } from "../src/lib/retellCall.ts";

const session = {
  call_id: "call_abc",
  access_token: "tok",
  transport: "gateway",
  ice_servers: [{ urls: ["stun:s:3478"] }, { urls: "turn:t:443", username: "u", credential: "c" }],
  expires_at: 1_800_000_000_000,
};

test("v3 session maps to the SDK's StartCallConfig with exact camelCase field names", () => {
  assert.deepEqual(buildStartCallConfig(session), {
    accessToken: "tok",
    callId: "call_abc",
    transport: "gateway",
    iceServers: session.ice_servers,
    sampleRate: 24000,
  });
});

test("ice servers keep username and credential untouched (TURN needs them)", () => {
  const cfg = buildStartCallConfig(session);
  assert.deepEqual(cfg.iceServers?.[1], { urls: "turn:t:443", username: "u", credential: "c" });
});

test("no ice servers means the SDK's own defaults, not an empty list override", () => {
  assert.equal("iceServers" in buildStartCallConfig({ ...session, ice_servers: [] }), false);
  assert.equal("iceServers" in buildStartCallConfig({ ...session, ice_servers: undefined }), false);
});

test("a livekit session passes its url through", () => {
  const cfg = buildStartCallConfig({ ...session, transport: "livekit", url: "wss://x.test" });
  assert.equal(cfg.transport, "livekit");
  assert.equal(cfg.url, "wss://x.test");
});

test("an incomplete or unknown-transport response is refused instead of silently defaulting to livekit", () => {
  assert.throws(() => buildStartCallConfig({ ...session, transport: undefined }), /incomplete/);
  assert.throws(() => buildStartCallConfig({ ...session, transport: "carrier-pigeon" }), /incomplete/);
  assert.throws(() => buildStartCallConfig({ ...session, call_id: "" }), /incomplete/);
  assert.throws(() => buildStartCallConfig({ ...session, access_token: "" }), /incomplete/);
});

// ---- the installed SDK really has this contract (guards a future SDK upgrade) ----
const sdk = new URL("../node_modules/retell-client-js-sdk/dist/", import.meta.url);

test("installed SDK's StartCallConfig declares accessToken, callId, transport, iceServers, url and sampleRate", () => {
  const dts = readFileSync(new URL("transport.d.ts", sdk), "utf8");
  const body = dts.slice(dts.indexOf("interface StartCallConfig"), dts.indexOf("}", dts.indexOf("interface StartCallConfig")));
  for (const f of ["accessToken: string", "transport?: TransportKind", "callId?: string", "iceServers?: RTCIceServer[]", "url?: string", "sampleRate?: number"]) {
    assert.ok(body.includes(f), `StartCallConfig is missing ${f}`);
  }
  assert.ok(dts.includes('type TransportKind = "livekit" | "gateway"'));
});

test("installed SDK emits the events StepTest listens for, and its gateway transport needs callId", () => {
  const js = readFileSync(new URL("index.modern.mjs", sdk), "utf8");
  for (const ev of ["call_started", "call_ended", "agent_start_talking", "agent_stop_talking", '"error"']) {
    assert.ok(js.includes(`emit(${ev.startsWith('"') ? ev : `"${ev}"`}`), `SDK no longer emits ${ev}`);
  }
  assert.ok(js.includes("callId is required for the gateway transport"));
  // with no transport given the SDK falls back to livekit: that is why transport is always passed explicitly
  assert.ok(js.includes('defaultTransport=t.defaultTransport||"livekit"'));
});

test("StepTest hands the SDK the built config and no longer passes only the token", () => {
  const src = readFileSync(new URL("../src/components/wizard/StepTest.tsx", import.meta.url), "utf8");
  assert.ok(src.includes("client.startCall(buildStartCallConfig(session))"));
  assert.ok(!src.includes("startCall({ accessToken"));
  for (const ev of ["call_started", "call_ended", "agent_start_talking", "agent_stop_talking", '"error"']) {
    assert.ok(src.includes(ev.replace(/"/g, "")), `StepTest lost its ${ev} handler`);
  }
});
