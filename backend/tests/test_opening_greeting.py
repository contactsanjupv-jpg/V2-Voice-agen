"""
One opening greeting per call. Retell speaks `begin_message` (start_speaker=agent) and then hands the turn to the
model; the general prompt must tell the model that opening is already said, and must never carry a greeting of its own.
Checked on the exact payloads sent to Retell (a recorder stands in for it; no real call is made).
"""

import re

import pytest

from app.config import get_settings
from app.services.disclosure import opening_message
from tests.test_advanced_settings import _patch, _setup
from tests.test_agent_config import _agent_row, _body, _put, fake  # noqa: F401
from tests.test_inbound_call_resolution import _retell_key  # noqa: F401  (autouse)

settings = get_settings()
SCHED = {"enabled": True, "weekly": {"mon": [["09:00", "17:00"]]}}
OWNER_GREETING = (
    "Thanks for calling Sunrise Dental, how can I help?"  # what the wizard/owner stores
)
ALREADY_SAID = re.compile(r"ALREADY been said", re.I)


@pytest.fixture(autouse=True)
def _flag(monkeypatch):
    monkeypatch.setattr(settings, "AFTER_HOURS_ROUTING_ENABLED", True)


def _llm_bodies(fake):  # noqa: F811
    """Every LLM payload sent to Retell, create and update, in order."""
    return [
        c[2]
        for c in fake.calls
        if c[1] == "/create-retell-llm" or c[1].startswith("/update-retell-llm/")
    ]


def _normal_and_closed(fake):  # noqa: F811
    creates = [c[2] for c in fake.to("/create-retell-llm")]
    assert len(creates) == 2, "expected the normal agent and the after-hours twin"
    closed = next(
        b
        for b in creates
        if "no one is available" in b["general_prompt"].lower()
        or "closed right now" in b["general_prompt"].lower()
    )
    normal = next(b for b in creates if b is not closed)
    return normal, closed


def _assert_one_opening(body, business="Sunrise Dental"):
    begin = body["begin_message"]
    assert (
        body["start_speaker"] == "agent"
    )  # Retell speaks begin_message exactly once, first
    assert (
        business in begin and begin.lower().count("how can i help") <= 1
    )  # disclosure + at most the owner's one greeting, nothing said twice
    assert (
        begin.count("transcribed") == 1
        and begin.lower().count("thanks for calling") <= 1
    )
    prompt = body["general_prompt"]
    assert (
        begin not in prompt
    )  # the opening text is never copied into the prompt for the model to say again
    assert "thanks for calling" not in prompt.lower()  # nor any greeting phrase
    assert not re.search(
        r"(greet\b|welcome|answer the (phone|call)|begin the call|start the call|open the call|say hello)",
        prompt.replace("Never greet the caller again", ""),
        re.I,
    )
    assert (
        ALREADY_SAID.search(prompt) and "Never greet the caller again" in prompt
    )  # model is told the opening is done


def test_the_normal_receptionist_opens_exactly_once(fake):  # noqa: F811
    _setup(fake)
    (body,) = [c[2] for c in fake.to("/create-retell-llm")]
    _assert_one_opening(body)
    assert body["begin_message"].endswith(OWNER_GREETING)


def test_the_after_hours_twin_opens_exactly_once_and_stays_closed(fake):  # noqa: F811
    c, org, biz, _v = _setup(fake)
    assert (
        _patch(
            c,
            org,
            biz,
            {
                "name": "Sunrise Dental",
                "timezone": "America/Chicago",
                "hours_schedule": SCHED,
            },
        ).status_code
        == 200
    )
    normal, closed = _normal_and_closed(fake)
    _assert_one_opening(closed)
    assert (
        closed["begin_message"] == normal["begin_message"]
    )  # same compliant opener on both agents
    assert "transfer_to_human" not in str(
        closed["general_tools"]
    )  # existing closed-hours behaviour kept
    assert "closed right now" in closed["general_prompt"]


def test_the_prompt_alone_never_greets_and_keeps_normal_behaviour(fake):  # noqa: F811
    _setup(fake)
    (body,) = [c[2] for c in fake.to("/create-retell-llm")]
    p = body["general_prompt"]
    assert "Sunrise Dental" in p  # business name still personalises the profile
    assert (
        "You are an AI assistant, never a person" in p
        and "transcribed and may also be recorded" in p
    )  # honesty rules kept
    assert "answer what they actually say" in p.replace(
        "Wait for them, then answer what they actually say",
        "answer what they actually say",
    )
    assert "YOUR TASKS" in p and "end_call" in str(body["general_tools"])


def test_the_disclosure_is_still_delivered_through_begin_message(fake):  # noqa: F811
    _setup(fake)
    (body,) = [c[2] for c in fake.to("/create-retell-llm")]
    begin = body["begin_message"]
    assert begin.startswith(
        "Hi, I'm the AI assistant for Sunrise Dental. This call is transcribed."
    )
    assert begin == opening_message("Sunrise Dental", OWNER_GREETING)


def test_resaving_and_resyncing_never_reintroduces_a_second_greeting(fake):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    assert (
        _patch(
            c,
            org,
            biz,
            {
                "name": "Sunrise Dental",
                "timezone": "America/Chicago",
                "hours_schedule": SCHED,
            },
        ).status_code
        == 200
    )
    assert (
        _patch(
            c,
            org,
            biz,
            {"name": "Sunrise Dental", "description": "New description for the clinic"},
        ).status_code
        == 200
    )  # profile edit re-syncs
    assert (
        _put(c, org, biz, _body(voice, personality="professional")).status_code == 200
    )  # receptionist re-save
    bodies = _llm_bodies(fake)
    assert len(bodies) >= 5
    openings = {b["begin_message"] for b in bodies}
    assert openings == {
        opening_message("Sunrise Dental", OWNER_GREETING)
    }  # identical on every create/update
    for b in bodies:
        _assert_one_opening(b)


def test_a_changed_greeting_is_used_as_typed_and_never_overwritten_by_a_sync(fake):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    mine = "Sunrise Dental dental desk, what can I do for you?"
    assert _put(c, org, biz, _body(voice, greeting=mine)).status_code == 200
    assert _agent_row(biz).greeting == mine
    assert (
        _patch(
            c, org, biz, {"name": "Sunrise Dental", "description": "Another edit"}
        ).status_code
        == 200
    )  # a later sync
    assert _agent_row(biz).greeting == mine  # stored exactly as typed
    body = _llm_bodies(fake)[-1]
    assert body["begin_message"] == opening_message("Sunrise Dental", mine)
    assert (
        mine not in body["general_prompt"]
    )  # the greeting lives in begin_message only


# ---------------- default opening: the short notice alone; a custom greeting follows it ----------------

NOTICE = "Hi, I'm the AI assistant for Sunrise Dental. This call is transcribed."
MINE = "You've reached the front desk, go ahead."


def _empty(voice):
    return _body(voice, greeting=None)


@pytest.mark.parametrize("empty", [None, "", "   "], ids=["none", "empty", "blank"])
def test_an_empty_greeting_opens_with_the_short_notice_and_nothing_else(fake, empty):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    assert _put(c, org, biz, _body(voice, greeting=empty)).status_code == 200
    body = _llm_bodies(fake)[-1]
    assert body["begin_message"] == NOTICE
    low = body["begin_message"].lower()
    assert (
        "how can i help" not in low
        and "record" not in low
        and "thanks for calling" not in low
    )
    _assert_one_opening(body)


def test_a_custom_greeting_is_appended_after_the_notice_and_nothing_else_is_added(fake):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    assert _put(c, org, biz, _body(voice, greeting=MINE)).status_code == 200
    body = _llm_bodies(fake)[-1]
    assert body["begin_message"] == f"{NOTICE} {MINE}"
    assert (
        "how can i help you today" not in body["begin_message"].lower()
    )  # the old default is gone
    assert MINE not in body["general_prompt"]


def test_changing_between_empty_and_custom_never_stacks_greetings(fake):  # noqa: F811
    c, org, biz, voice = _setup(fake)  # starts with the wizard-style owner greeting
    seq = [
        (MINE, f"{NOTICE} {MINE}"),
        (None, NOTICE),
        (OWNER_GREETING, f"{NOTICE} {OWNER_GREETING}"),
        ("", NOTICE),
    ]
    for typed, expected in seq:
        assert _put(c, org, biz, _body(voice, greeting=typed)).status_code == 200
        body = _llm_bodies(fake)[-1]
        assert body["begin_message"] == expected
        assert body["begin_message"].count("AI assistant") == 1
        _assert_one_opening(body)


def test_the_after_hours_twin_uses_the_same_default_and_custom_openings(fake):  # noqa: F811
    for typed, expected in ((None, NOTICE), (MINE, f"{NOTICE} {MINE}")):
        fake.calls.clear()
        c, org, biz, voice = _setup(fake)
        assert _put(c, org, biz, _body(voice, greeting=typed)).status_code == 200
        assert (
            _patch(
                c,
                org,
                biz,
                {
                    "name": "Sunrise Dental",
                    "timezone": "America/Chicago",
                    "hours_schedule": SCHED,
                },
            ).status_code
            == 200
        )
        normal, closed = _llm_bodies(fake)[
            -2:
        ]  # the save re-synced the normal agent, then created the twin
        assert "closed right now" in closed["general_prompt"]
        assert normal["begin_message"] == closed["begin_message"] == expected
        _assert_one_opening(closed)


def test_resyncing_keeps_the_default_opening_and_the_prompt_stays_silent(fake):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    assert _put(c, org, biz, _empty(voice)).status_code == 200
    assert (
        _patch(
            c, org, biz, {"name": "Sunrise Dental", "description": "Edited again"}
        ).status_code
        == 200
    )
    assert (
        _patch(
            c,
            org,
            biz,
            {
                "name": "Sunrise Dental",
                "timezone": "America/Chicago",
                "hours_schedule": SCHED,
            },
        ).status_code
        == 200
    )
    assert (
        _put(
            c, org, biz, _body(voice, greeting=None, personality="concise")
        ).status_code
        == 200
    )
    recent = _llm_bodies(fake)[-4:]
    assert len(recent) == 4 and {b["begin_message"] for b in recent} == {NOTICE}
    assert (
        _agent_row(biz).greeting is None
    )  # nothing was written back into the owner's field
    for b in recent:
        assert "thanks for calling" not in b[
            "general_prompt"
        ].lower() and ALREADY_SAID.search(b["general_prompt"])


def test_a_pasted_preview_from_either_wording_is_not_said_twice():
    earlier = "Hi, I'm the AI assistant for Sunrise Dental. This call is transcribed and may also be recorded."
    assert opening_message("Sunrise Dental", NOTICE) == NOTICE
    assert opening_message("Sunrise Dental", f"{NOTICE} {MINE}") == f"{NOTICE} {MINE}"
    assert (
        opening_message("Sunrise Dental", earlier) == NOTICE
    )  # old preview pasted back: tightened, not doubled
    assert opening_message("Sunrise Dental", f"{earlier} {MINE}") == f"{NOTICE} {MINE}"


def test_the_prompt_still_tells_the_model_the_opening_is_done_and_never_adds_a_greeting(
    fake,
):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    assert _put(c, org, biz, _empty(voice)).status_code == 200
    p = _llm_bodies(fake)[-1]["general_prompt"]
    assert "Never greet the caller again" in p and "invitation to speak" not in p
    assert "how can i help" not in p.lower() and "AI assistant, never a person" in p
