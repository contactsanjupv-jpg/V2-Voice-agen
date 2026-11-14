# Business timezone (Level 8B)

Each business has an optional IANA timezone (`businesses.timezone`, e.g. `America/Chicago`). It is set in onboarding
(defaults to the browser's zone) and in Settings > Business > Timezone.

- **Validation**: region/city names from the installed tz database only. Abbreviations (`CST`), fixed offsets
  (`Etc/GMT+5`), `UTC` and anything with odd characters are rejected with a 422. Fixed offsets are refused because
  they ignore daylight saving.
- **Not set** (existing businesses): nothing is guessed. Owner emails show an explicit `UTC` time and the AI prompt has
  no clock section. Setting the timezone fixes both.
- **AI prompt**: the prompt gains a `CURRENT DATE AND TIME` section containing `{{current_time_<zone>}}`, a Retell
  built-in dynamic variable that Retell fills with the live local time at the start of every call. Saving the profile
  re-syncs the agent, so existing agents get the new prompt when the owner sets or changes the timezone.
- **Owner emails** (lead received, payment-failed cutoff) show business-local time with the right abbreviation for
  that instant (`CST` vs `CDT`). Stored instants stay UTC.

## Live check (UNVERIFIED offline, needs a real Retell account)
1. Set a timezone on a test business and save (this re-syncs the agent).
2. In Retell, open the agent's LLM prompt and confirm `{{current_time_America/Chicago}}` is present.
3. Place a test call and ask "what day and time is it there?". It should answer in the business's local time.
   If the variable shows up literally in the answer, Retell does not support that variable name for the account:
   tell us and we will switch to a per-call dynamic variable passed at call start.
