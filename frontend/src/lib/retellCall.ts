import type { StartCallConfig, TransportKind } from "retell-client-js-sdk";

/**
 * What POST /agents/{id}/test-call returns for the browser (Retell v3 web call). The SDK needs more than the token:
 * the gateway transport refuses to connect without `callId`, and with no `transport` the SDK silently defaults to
 * "livekit", which cannot use a v3 token.
 */
export type TestCallSession = {
  call_id: string;
  access_token: string;
  transport?: string;
  ice_servers?: RTCIceServer[];
  expires_at?: number;
  url?: string;
};

const TRANSPORTS: readonly TransportKind[] = ["gateway", "livekit"];

/** Maps the API response to the installed SDK's StartCallConfig (exact camelCase field names). Throws on an unusable response. */
export function buildStartCallConfig(session: TestCallSession, sampleRate = 24000): StartCallConfig {
  const transport = TRANSPORTS.find((t) => t === session.transport);
  if (!session.access_token || !session.call_id || !transport) {
    throw new Error("The voice provider's response was incomplete");
  }
  const config: StartCallConfig = {
    accessToken: session.access_token,
    callId: session.call_id,
    transport,
    sampleRate,
  };
  if (session.ice_servers?.length) config.iceServers = session.ice_servers;
  if (session.url) config.url = session.url;
  return config;
}
