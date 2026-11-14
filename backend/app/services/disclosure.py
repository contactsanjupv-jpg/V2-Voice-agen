"""
Mandatory caller disclosure (Level 8D). Every call opens with a server-generated notice that the caller is talking to
an AI assistant and that the call is transcribed. The owner cannot remove or switch it off:
it is added when the Retell `begin_message` is built (agent_service.build_agent_config), for the normal receptionist,
the after-hours twin and browser test calls alike. The owner's greeting is stored exactly as typed and follows it.

What this is NOT: a consent flow. Atla stores transcripts, so "transcribed" is a fact. Whether Retell also records
audio on our account is UNVERIFIED (Atla sets no recording field on the agent); the opening makes no recording claim. An
opening announcement is notice only; we do not ask the caller to agree, and nothing here claims it satisfies every
state's recording-consent law.
"""

import re


def opening_disclosure(business_name: str) -> str:
    return f"Hi, I'm the AI assistant for {business_name}. This call is transcribed."


def _earlier_disclosure(business_name: str) -> str:
    """The wording before the recording claim was dropped; an owner may paste it back in."""
    return (
        f"Hi, I'm the AI assistant for {business_name}. "
        "This call is transcribed and may also be recorded."
    )


def opening_message(business_name: str, greeting: str | None) -> str:
    """
    What the caller hears first: the mandatory disclosure, then the owner's greeting if
    they wrote one. With no greeting it is the disclosure alone: there is no default
    greeting, so nothing else is said by us or by the LLM prompt.
    """
    disclosure = opening_disclosure(business_name)
    text = (greeting or "").strip()
    if text.startswith(disclosure):  # pasted the preview back in: don't say it twice
        return text
    earlier = _earlier_disclosure(business_name)
    if text.startswith(earlier):
        text = text[len(earlier) :].strip()
    return f"{disclosure} {text}" if text else disclosure


# Deliberately narrow: explicit claims of being human, or that the call is not transcribed/recorded. Not moderation.
_DECEPTIVE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bi(?:'m| am)\s+(?:a\s+|an\s+)?(?:real|live|actual|living)\s+(?:person|human|human being|agent|receptionist)\b",
        r"\b(?:i(?:'m| am)|this is|you(?:'re| are)\s+(?:talking|speaking)\s+(?:to|with))\s+(?:a\s+|an\s+)?"
        r"(?:real\s+|live\s+)?(?:human|person)\b",
        r"\bnot\s+(?:a|an)\s+(?:robot|bot|ai|machine|computer)\b",
        r"(?:\bnot|n't)\s+(?:being\s+)?(?:recorded|transcribed)\b",
        r"\bno\s+(?:recording|transcription)\b",
    )
]

GREETING_REJECTED = (
    "Your greeting can't say or imply that the caller is talking to a human, or that the call isn't transcribed or "
    "recorded. Atla adds an AI and transcription notice to the start of every call, and it can't be removed."
)


def deceptive_greeting_claim(text: str | None) -> bool:
    if not text:
        return False
    t = text.replace("’", "'").replace("‘", "'")
    return any(p.search(t) for p in _DECEPTIVE)
