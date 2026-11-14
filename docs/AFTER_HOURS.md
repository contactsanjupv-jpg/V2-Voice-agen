# After-hours behaviour (Level 8C)

## What it does
An owner can turn on **structured opening hours** (weekly windows, several per day, plus closed/special dates) in
Settings → Business profile → *After-hours handling*. When the feature is on:

* the number's **default** receptionist is the **closed twin**: same voice and knowledge, **no transfer tool**, prompt says
  "closed, take a message, never transfer";
* each inbound call asks `POST /webhooks/retell/inbound`; the server answers with the **normal** receptionist only when it
  positively confirms the business is open *at that instant* in the business timezone;
* anything else (closed, no/invalid timezone, corrupt schedule, missing twin, database error, bad payload, timeout, webhook
  down) ends on the closed twin. There is no code path from "unknown" or "error" to the normal receptionist;
* leads get `received_after_hours` (True/False/NULL) from the **call's own start time**, stored once, never recomputed; the
  8A lead email adds "After hours: this call came in while your business was closed." when True.

A business that has not enabled a schedule is untouched: no twin, no webhook, same prompt, same lead email.

## One engine, two adapters
```
hours_schedule.evaluate  ->  inbound_routing.route_inbound_call  ->  render_response
        ^                                ^
   RETELL adapter: POST /webhooks/retell/inbound (signed)      SIMULATED adapter: POST /api/v1/dev/simulate-inbound
                                                               (local sandbox only; 404 elsewhere; fake numbers, pretend clock)
```
Both adapters call `handle_inbound_payload`; `tests/test_after_hours.py` runs every scenario through both and requires
identical responses. Lead creation and email are the existing call pipeline for either source.

## Switching on
1. Set `AFTER_HOURS_ROUTING_ENABLED=true` (default false). Where it goes matters:
   - `config.py` reads `backend/.env`, **not** `.env.sandbox`. For a sandbox, either `export AFTER_HOURS_ROUTING_ENABLED=true`
     in the shell that starts the backend, or start it with `--env-file .env.sandbox`.
   - Settings are cached at startup (`get_settings()` is `lru_cache`d), so **restart the backend** after changing it.
   - The Settings page shows a notice when hours are on but the server switch is off.
2. Owner saves hours (and a timezone) in Settings. Saving re-syncs the agent, which creates/updates the twin.
   If hours were saved while the switch was off, turn it on, restart, and save again (even unchanged): a save now creates the
   missing twin. The receptionist shows "changes not applied" until the twin exists. If Retell fails part-way, the partial
   twin is kept and the next save resumes it (no duplicate).
   The closed test call (`?variant=closed`) answers 409 and names the cause: switch off, hours not enabled, or twin not created.
3. *Go live* (activation) points the number's default at the twin and registers `<BACKEND_PUBLIC_URL>/webhooks/retell/inbound`.
   Turning the schedule off and going live again restores the normal agent and clears the webhook.
4. Try it in the browser: the test call endpoint takes `?variant=closed` to talk to the twin (the normal agent is the default).
   Browser calls cannot exercise the transfer itself.

## Simulate locally
With the unmetered sandbox settings active (ENV=development, PADDLE_ENV=sandbox, clone database), signed in:
`POST /api/v1/dev/simulate-inbound {"to_number": "<a number in your DB>", "at": "2026-11-10T21:00:00-06:00"}` returns the
request it built, the Retell-shaped response and the decision.

## UNVERIFIED against live Retell (do not claim otherwise)
* **G1** the phone-number field shape (`inbound_agent_id` vs `inbound_agents`). Reads accept either; writes still use
  `inbound_agent_id` like the rest of the app. Run the read-only `get-phone-number` check first.
* **G2** whether Retell signs `call_inbound` requests with `X-Retell-Signature`. We verify it and reject unsigned requests;
  if Retell does not sign this webhook, every call falls back to the default (closed) agent, which is safe but means
  nobody ever gets the normal agent. Verify with one real call.
* That Retell honours `override_agent_id` and echoes `metadata` in the `call_inbound` response, and its 10 s timeout behaviour.
* Nothing here has made a call to Retell. The simulation proves our code under Retell's documented contract only.

## Known limits
* Routing is decided when the call rings; Retell's webhook has no start time, so "now" is receipt time.
* No periodic drift sweep: if someone re-points a number by hand, "Go live" (or the live check, which expects the twin) shows it.
* No holiday library; the owner enters closed dates.
* Overnight windows are entered as two windows (22:00-24:00, then 00:00-02:00 the next day).
* Pausing a number (entitlement) still unassigns it as before.

## Rollback
Set `AFTER_HOURS_ROUTING_ENABLED=false` (calls stop using the webhook logic; numbers keep defaulting to the twin until
"Go live" is run again — safe, but no transfers). To revert fully: downgrade migration `e6b8d0f24a68` and delete the twin
agents/LLMs in Retell by hand.
