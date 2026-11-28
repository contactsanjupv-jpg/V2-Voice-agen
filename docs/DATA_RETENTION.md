# Data retention (what Atla deletes, what it cannot, and what you must decide)

This describes engineering behavior. It is **not** legal advice and makes **no compliance claim**.

## What Atla stores about a call
| Data | Where | Pruned by |
|---|---|---|
| Transcript | `call_transcripts` (own table) | `TRANSCRIPT_RETENTION_DAYS` |
| Summary | `calls.summary` | `TRANSCRIPT_RETENTION_DAYS` |
| Recording | **Not stored by Atla.** `calls.recording_storage_key` exists but nothing writes it; audio, if any, lives at Retell | key cleared by `TRANSCRIPT_RETENTION_DAYS` |
| Caller phone number | `calls.caller_number` | `CALLER_DATA_RETENTION_DAYS` |
| Call facts (time, duration, status, cost) | `calls` | kept: usage, credits and billing reconcile against them |
| Leads (name, phone, notes) | `leads` | **not pruned**: they are the customer's business records; the customer or account deletion removes them |

## Settings (all default to "keep", so nothing is deleted until you decide)
`TRANSCRIPT_RETENTION_DAYS`, `CALLER_DATA_RETENTION_DAYS` (0 = keep, else 1-3650), `RETENTION_BATCH_SIZE`, `RETELL_DATA_RETENTION_DAYS` (1-730 or unset).
Age is measured from the call start. The worker runs the pass at most once an hour (`data_retention` job). It is idempotent, batched,
scoped by `organization_id` on every statement, one transaction per organization, and a failing organization is recorded and retried next pass
without stopping the others or any billing job.

## Audit trail
`data_retention_runs`: organization, kind, cutoff, rows affected, ok/failed. **Counts only**, never content, numbers or names. Removed with the organization.

## Retell (the part Atla cannot guarantee)
Per Retell's documentation (docs.retellai.com/accounts/data-retention), recordings, transcripts and logs are kept **forever by default**; retention
is a per-agent setting `data_storage_retention_days` (1-730 days or null), and Retell does not offer an API for Atla to delete a past call's data.
Setting `RETELL_DATA_RETENTION_DAYS` makes Atla send that field when it creates or updates an agent. **Not verified live** (no Retell credentials were
available): after setting it, confirm in the Retell dashboard that the agent shows the retention and that a test call's data expires. Agents
that already exist are updated the next time Atla updates them; check them in the dashboard. Whether recording is enabled for your account is a
Retell account setting Atla does not read: check it.

## Decisions that are yours (and your lawyer's)
1. How long to keep transcripts/summaries, and caller numbers (the code is ready; the numbers are not mine to choose).
2. Whether to record audio at all, and the consent wording per state (two-party-consent states) and the AI disclosure text.
3. What your privacy policy promises customers about retention and deletion, and the processor terms with Retell and Paddle.
4. Whether leads need their own retention window.

## Verify after you set the values
Run a pass against a copy of production data, read `data_retention_runs`, and confirm the counts match what you expected before enabling in production.
