# AI and transcription disclosure (Level 8D)

## What every caller hears first
> Hi, I'm the AI assistant for {business}. This call is transcribed. {owner's greeting, only if the owner wrote one}

* Built on the server when the Retell `begin_message` is created (`app/services/disclosure.py`, used by
  `agent_service.build_agent_config`). It applies to the normal receptionist, the 8C after-hours twin and browser test calls.
* It is mandatory. There is no setting to turn it off or shorten it, and the owner's greeting is stored exactly as typed
  and only follows it. If an owner pastes the whole opening back into the greeting box it is not said twice.
* If the greeting is empty the opening is the disclosure alone: there is no default greeting, from us or from the LLM prompt.
  The setup wizard no longer pre-fills one either (a greeting saved by an older version is the owner's own text and is still
  said after the disclosure until they clear it).
* Greetings that say the caller is talking to a human, "I'm not a bot", or that the call is "not recorded/transcribed" are
  rejected with an error (not rewritten). The check is deliberately narrow: it is not general moderation.

## What the receptionist is told (`SYSTEM_RULES`, `objection_rule`)
Never claim or imply it is human; say plainly it is an AI if asked; never deny transcription or possible recording; never
suggest leaving a message avoids transcription. If a caller objects to the AI or to transcription/recording it must stop
collecting their name, number or message, offer a transfer **only** if a transfer tool really exists on that agent (not
on the after-hours twin), otherwise point them to the business's own contact details, and end the call.
These are instructions to a language model: they are tested as configuration, not as spoken behaviour.

## Recording: what is and is not true
* **Transcripts are stored** by Atla, so "transcribed" is a fact.
* Atla sets **no recording field** (and no consent or data-storage field) on the Retell agent. **That absence does not mean
  recording is off.** Retell's `everything` data-storage mode includes recordings and transcripts, and leaving the
  field out may default to `everything`. Whether audio is recorded for our account is **UNVERIFIED**: check the actual
  Retell account/agent setting before launch. The opening says only "transcribed"
  and makes no recording claim; the receptionist is still told to say "may also be recorded" if a caller asks. Confirm the
  Retell setting before relying on "Atla does not record audio".
* This is **not a consent flow.** The caller is told; they are not asked to agree, and the call continues.
  Atla does not claim that an opening announcement meets every US state's recording-consent rules (some states require every party's
  consent). That is a legal question for counsel, not something this patch decides.
* The one stop for a caller who objects is the prompt rule above; there is no telephony-level "press 1 to consent".

## Existing businesses
Saved receptionists only pick this up when they are synced. Saving the receptionist does it for that business. To refresh
all of them deliberately:

```
python -m app.workers.resync_agents                                  # dry run (default): lists agent + organization ids
python -m app.workers.resync_agents --org <organization uuid>        # dry run for one organization
python -m app.workers.resync_agents --apply --org <organization uuid>   # sync ONE organization
python -m app.workers.resync_agents --apply --all-orgs                  # sync every organization (explicit opt-in)
```
`--apply` refuses to run without `--org` or `--all-orgs`. A dry run makes no Retell calls and writes nothing. It is never
run automatically and nothing in this patch changes data.

## Verify by ear (one browser test call)
Start a test call from the wizard or Settings and listen: the first sentence must be the disclosure, then the greeting. Ask
"Am I talking to a real person?" (expect: it is an AI assistant) and "Is this being recorded?" (expect: transcribed, may be
recorded, no denial). Say you don't want to be recorded (expect: no name/number requested, pointed to the business). Spoken
behaviour has not been verified here.
