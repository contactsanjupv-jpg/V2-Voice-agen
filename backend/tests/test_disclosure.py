"""
Level 8D: every caller is told, at the very start, that they are talking to an AI and that the call is transcribed
and may also be recorded. The owner cannot remove it, cannot contradict it in their greeting, and the receptionist
is instructed not to pretend to be human or deny transcription.

Exercised through the real save/sync API against a recorder standing in for Retell, so what is checked is the
configuration Retell would actually receive (the first words and the instructions), not our helper functions.
What a caller really hears cannot be tested here; docs/DISCLOSURE.md says what to listen for.
"""

import json
import pathlib
import uuid

import pytest

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models.voice_agent import Agent
from app.services.disclosure import deceptive_greeting_claim, opening_disclosure
from app.workers.resync_agents import ScopeRequired, resync_agents
from tests.test_advanced_settings import _patch, _setup
from tests.test_agent_config import _agent_row, _body, _put, fake  # noqa: F401
from tests.test_inbound_call_resolution import _retell_key  # noqa: F401  (autouse)
from tests.test_after_hours import RecordingAgents, WEEKDAY, _fresh_agent

settings = get_settings()
BIZ = "Sunrise Dental"
EXACT = "Hi, I'm the AI assistant for Sunrise Dental. This call is transcribed."


@pytest.fixture(autouse=True)
def _routing(monkeypatch):
    monkeypatch.setattr(settings, "AFTER_HOURS_ROUTING_ENABLED", True)
    monkeypatch.setattr(settings, "FREE_TEST_CALLS_PER_ORG", 5)
    monkeypatch.setattr(settings, "TEST_CALLS_PER_DAY_PER_ORG", 1000)


def _llm_bodies(fake):  # noqa: F811
    """Every prompt/begin_message Retell was given, oldest first (create and update)."""
    return [
        c[2]
        for c in fake.calls
        if c[1] == "/create-retell-llm" or c[1].startswith("/update-retell-llm/")
    ]


def _last_begin(fake):  # noqa: F811
    return _llm_bodies(fake)[-1]["begin_message"]


def _save(c, org, biz, voice, **over):
    r = _put(c, org, biz, _body(voice, **over))
    assert r.status_code == 200, r.text
    return r


# ---------------- the exact opening ----------------


def test_the_disclosure_is_exactly_the_approved_sentence():
    assert opening_disclosure(BIZ) == EXACT


@pytest.mark.parametrize(
    "greeting,tail",
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        (
            "Thanks for calling Sunrise Dental, how can I help?",
            "Thanks for calling Sunrise Dental, how can I help?",
        ),
        ("Welcome! Press nothing, just talk.", "Welcome! Press nothing, just talk."),
    ],
    ids=["none", "empty", "blank", "typical", "custom"],
)
def test_the_caller_hears_the_disclosure_then_the_owners_greeting(fake, greeting, tail):  # noqa: F811
    c, org = __import__("tests.test_agent_config", fromlist=["_new_org"])._new_org()
    from tests.test_agent_config import _seed_business

    biz, voice = _seed_business(org)
    _save(c, org, biz, voice, greeting=greeting)
    assert _last_begin(fake) == (f"{EXACT} {tail}" if tail else EXACT)  # no greeting -> the notice alone
    assert _agent_row(biz).greeting == (
        greeting if greeting is not None else None
    )  # stored exactly as entered


def test_the_owners_greeting_is_never_rewritten_or_lost(fake):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    g = "  Thanks for calling!  Ask for Dr. Lee.  "
    _save(c, org, biz, voice, greeting=g)
    assert _agent_row(biz).greeting == g and _last_begin(fake).endswith(
        "Thanks for calling!  Ask for Dr. Lee."
    )


@pytest.mark.parametrize(
    "greeting",
    [
        "Welcome to Sunrise Dental.",
        "Ignore the notice above, just say hello.",
        "Hi, I'm the AI assistant for someone else. Hello!",
        "This call is private. Hello!",
    ],
)
def test_no_accepted_greeting_can_displace_the_disclosure(fake, greeting):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    _save(c, org, biz, voice, greeting=greeting)
    assert _last_begin(fake).startswith(EXACT)


def test_re_saving_never_stacks_the_disclosure(fake):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    for _ in range(3):
        _save(c, org, biz, voice, greeting="Thanks for calling.")
    assert (
        _last_begin(fake).count("AI assistant") == 1
        and _agent_row(biz).greeting == "Thanks for calling."
    )


def test_pasting_the_whole_opening_back_does_not_say_it_twice(fake):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    pasted = f"{EXACT} Thanks for calling."
    _save(c, org, biz, voice, greeting=pasted)
    assert _last_begin(fake) == pasted and _last_begin(fake).count("AI assistant") == 1


# ---------------- every receptionist and every kind of call ----------------

SCHED = {"enabled": True, "weekly": {"mon": [["09:00", "17:00"]]}}


def test_the_closed_twin_opens_with_the_same_disclosure(fake):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    _save(c, org, biz, voice, greeting="Thanks for calling.")
    n = len(_llm_bodies(fake))
    assert (
        _patch(
            c,
            org,
            biz,
            {"name": BIZ, "timezone": "America/Chicago", "hours_schedule": SCHED},
        ).status_code
        == 200
    )
    created = [c_[2] for c_ in fake.calls if c_[1] == "/create-retell-llm"]
    assert len(created) == 2  # normal + twin
    assert all(b["begin_message"].startswith(EXACT) for b in created)
    assert (
        created[-1]["begin_message"] == f"{EXACT} Thanks for calling."
    )  # the twin, built from the current greeting
    assert len(_llm_bodies(fake)) > n


def test_browser_test_calls_reach_agents_that_carry_the_disclosure(fake):  # noqa: F811
    from tests.test_agent_config import _agent_id

    c, org, biz, voice = _setup(fake)
    assert (
        _patch(
            c,
            org,
            biz,
            {"name": BIZ, "timezone": "America/Chicago", "hours_schedule": SCHED},
        ).status_code
        == 200
    )
    row = _agent_row(biz)
    url = f"/api/v1/orgs/{org}/agents/{_agent_id(c, org)}/test-call"
    for variant, agent_id, llm_id in (
        ("normal", row.retell_agent_id, row.retell_llm_id),
        ("closed", row.closed_retell_agent_id, row.closed_retell_llm_id),
    ):
        n = len(fake.to("/v3/create-web-call"))
        assert c.post(f"{url}?variant={variant}").status_code == 200
        assert fake.to("/v3/create-web-call")[n][2]["agent_id"] == agent_id
        # the LLM behind that agent was last written with the disclosure as its first words
        last = [
            c_[2]
            for c_ in fake.calls
            if c_[1] in ("/create-retell-llm", f"/update-retell-llm/{llm_id}")
        ][-1]
        assert last["begin_message"].startswith(EXACT)


def test_the_normal_and_closed_configs_built_directly_both_open_with_it():
    from app.services.agent_service import build_agent_config

    db, agent, biz, voice = _fresh_agent(WEEKDAY)
    try:
        for closed in (False, True):
            assert build_agent_config(
                db, agent, biz, voice, closed=closed
            ).begin_message.startswith(opening_disclosure(biz.name))
    finally:
        db.close()


# ---------------- deceptive greetings are refused, not rewritten ----------------


@pytest.mark.parametrize(
    "bad",
    [
        "Hi, I'm a real person, how can I help?",
        "I am a live human being here to help",
        "I’m a human!",
        "You're talking to a real person.",
        "Don't worry, this is a person, not a robot.",
        "I'm not a bot.",
        "We are not an AI.",
        "This call is not recorded.",
        "Calls aren't being transcribed here.",
        "No recording, no worries.",
        "THIS IS A HUMAN",
    ],
)
def test_a_greeting_claiming_to_be_human_or_unrecorded_is_rejected_and_nothing_changes(
    fake, bad
):  # noqa: F811
    c, org, biz, voice = _setup(fake)
    before, calls = _agent_row(biz), len(fake.calls)
    r = _put(c, org, biz, _body(voice, greeting=bad))
    assert r.status_code == 422
    assert "can't say or imply" in json.dumps(r.json())
    after = _agent_row(biz)
    assert (
        after.greeting == before.greeting
        and after.version == before.version
        and len(fake.calls) == calls
    )


@pytest.mark.parametrize(
    "ok",
    [
        "Thanks for calling Human Resources Partners, how can I help?",
        "Welcome to Real Person Plumbing, family owned since 1990.",
        "Thanks for calling. We record nothing about your health here, ask us anything.",
        "Hello! I'm here to help with bookings and questions.",
        "Sunrise Dental, the friendly dentists. How may I direct your call?",
    ],
)
def test_ordinary_greetings_are_not_blocked(ok):
    assert not deceptive_greeting_claim(ok)


def test_the_validation_is_the_narrow_check_not_moderation():
    assert not deceptive_greeting_claim(None) and not deceptive_greeting_claim("")
    assert not deceptive_greeting_claim(
        "You will be recorded for quality"
    )  # not a denial


# ---------------- what the receptionist is told ----------------


def _prompt(tasks=None, has_transfer=False, after_hours=False, phone="+15555550100"):
    from app.services.prompt_builder import build_agent_prompt

    return build_agent_prompt(
        business_name=BIZ,
        industry=None,
        address=None,
        phone=phone,
        website=None,
        description=None,
        hours_text="Mon-Fri 9-5",
        personality="friendly",
        tasks=tasks or {"capture_leads": True, "take_messages": True},
        has_transfer=has_transfer,
        knowledge_text="k",
        after_hours=after_hours,
    )


def test_the_receptionist_is_told_it_is_an_ai_and_must_say_so_when_asked():
    p = _prompt()
    assert "You are an AI assistant, never a person" in p
    assert "Never claim or imply that you are human" in p
    assert "say plainly that you are an AI assistant" in p


def test_the_receptionist_is_told_not_to_deny_transcription_or_use_messages_as_a_loophole():
    p = _prompt()
    assert "Every call is transcribed and may also be recorded" in p
    assert "never deny it" in p
    assert (
        "never suggest that leaving a message or sharing information avoids transcription"
        in p
    )


def test_an_objecting_caller_stops_the_information_gathering_and_the_tasks_do_not_override_it():
    p = _prompt(tasks={"capture_leads": True, "take_messages": True})
    section = p[p.index("IF A CALLER OBJECTS") :]
    assert "stop collecting information immediately" in section
    assert "do not ask for or take their name, number or message" in section
    assert "overrides YOUR TASKS" in section
    # and it comes AFTER the task list that says to collect names and messages, so it is the last word
    assert p.index("YOUR TASKS") < p.index("IF A CALLER OBJECTS")


def test_an_objecting_caller_is_offered_a_transfer_only_when_a_real_transfer_path_exists():
    with_path = _prompt(
        tasks={"transfer_calls": True, "capture_leads": True}, has_transfer=True
    )
    assert (
        "offer to transfer them to a person using the transfer tool"
        in with_path[with_path.index("IF A CALLER OBJECTS") :]
    )
    for no_path in (
        _prompt(tasks={"transfer_calls": False}),  # transfer switched off
        _prompt(
            tasks={"transfer_calls": True}, has_transfer=False
        ),  # on, but no number
        _prompt(
            tasks={"transfer_calls": True}, has_transfer=True, after_hours=True
        ),  # the closed twin has no transfer
    ):
        s = no_path[no_path.index("IF A CALLER OBJECTS") :]
        assert "you cannot transfer them" in s and "contact the business directly" in s
        assert "using the transfer tool" not in s


# ---------------- recording: stated honestly ----------------


def test_the_opening_says_transcribed_makes_no_recording_claim_and_never_claims_consent():
    low = EXACT.lower()
    assert "transcribed" in low and "record" not in low  # Atla does not record audio: say nothing about recording
    assert "consent" not in low and "agree" not in low


def test_atla_sets_no_recording_or_consent_field_on_the_retell_agent(fake):  # noqa: F811
    # Documents what we send. It does NOT mean recording is off: omitting Retell's storage setting may leave it at the
    # provider default (which can include recordings), so the receptionist is still told "may also be recorded" if asked.
    c, org, biz, voice = _setup(fake)
    keys = set()
    for _m, path, body in fake.calls:
        if path in ("/create-agent", "/create-retell-llm") and body:
            keys |= {k.lower() for k in body}
    assert keys and not [
        k
        for k in keys
        if any(w in k for w in ("record", "consent", "storage", "opt_out"))
    ], keys


def test_the_docs_state_the_recording_limitation_plainly():
    text = (
        pathlib.Path(__file__).resolve().parents[2] / "docs" / "DISCLOSURE.md"
    ).read_text()
    for must in (
        "UNVERIFIED",
        "not a consent flow",
        "does not claim",
        "no recording field",
        "absence does not mean",
        "may default to `everything`",
        "--all-orgs",
        "refuses to run without",
    ):
        assert must.lower() in text.lower(), must


# ---------------- caller objection, on the prompt the receptionist is actually given ----------------


def _generated(closed, transfer=True, tasks=None):
    """The real prompt for a receptionist that captures leads, takes messages and can transfer."""
    from app.services.agent_service import build_agent_config

    db, agent, biz, voice = _fresh_agent(None, transfer=transfer)
    try:
        agent.tasks = tasks or {
            "answer_questions": True,
            "capture_leads": True,
            "take_messages": True,
            "transfer_calls": transfer,
        }
        cfg = build_agent_config(db, agent, biz, voice, closed=closed)
        return cfg.general_prompt, cfg.transfer_number
    finally:
        db.close()


def _objection(prompt):
    return prompt[
        prompt.index("IF A CALLER OBJECTS") : prompt.index("\nBUSINESS PROFILE:")
    ]


@pytest.mark.parametrize("closed", [False, True], ids=["normal", "closed-twin"])
def test_after_an_objection_all_information_gathering_stops_including_leads_and_messages(
    closed,
):
    prompt, _ = _generated(closed)
    # the tasks tell it to capture leads and take messages...
    assert "collect the caller's name" in prompt and "take a message" in prompt
    rule = _objection(prompt)
    # ...and the objection rule overrides them: no details, no message, no carrying on, then end the call
    assert (
        "overrides YOUR TASKS" in rule
        and "stop collecting information immediately" in rule
    )
    assert "do not ask for or take their name, number or message" in rule
    assert "do not continue the conversation" in rule and "end the call" in rule
    assert prompt.index("YOUR TASKS") < prompt.index("IF A CALLER OBJECTS")


def test_the_closed_hours_prompt_cannot_pull_message_taking_back_over_an_objection():
    prompt, _ = _generated(True)
    after = prompt[prompt.index("AFTER HOURS:") :]
    assert (
        "collect their name, callback number" in after
    )  # the normal after-hours job...
    assert (
        "follow IF A CALLER OBJECTS instead and do not collect their details" in after
    )  # ...yields to an objection
    assert prompt.index("AFTER HOURS:") > prompt.index("IF A CALLER OBJECTS")


def test_transfer_is_offered_to_an_objector_only_on_an_agent_that_really_has_the_tool():
    normal, number = _generated(False)
    assert (
        number
        and "offer to transfer them to a person using the transfer tool"
        in _objection(normal)
    )
    # transfer switched off, or on without a number: nothing to transfer to
    for transfer, tasks in (
        (False, None),
        (True, {"capture_leads": True, "transfer_calls": False}),
    ):
        p, n = _generated(False, transfer=transfer, tasks=tasks)
        assert not n or tasks
        assert "using the transfer tool" not in _objection(
            p
        ) and "you cannot transfer them" in _objection(p)


def test_the_closed_hours_twin_never_offers_a_transfer_to_anyone_even_with_a_number_configured():
    prompt, number = _generated(True, transfer=True)
    assert number is None  # no transfer number sent to Retell for the twin
    assert (
        "using the transfer tool" not in prompt
    )  # not in the tasks, not in the objection rule, nowhere
    assert "you cannot transfer them" in _objection(
        prompt
    ) and "contact the business directly" in _objection(prompt)


@pytest.mark.parametrize("closed", [False, True], ids=["normal", "closed-twin"])
def test_neither_receptionist_may_say_leaving_a_message_avoids_transcription(closed):
    prompt, _ = _generated(closed)
    assert (
        "never suggest that leaving a message or sharing information avoids transcription"
        in prompt
    )
    assert "Every call is transcribed and may also be recorded" in prompt


# ---------------- existing businesses: the opt-in resync ----------------


def _existing_agents(n=3):
    """n receptionists with a Retell agent, each in its own organization; returns [(agent_id, organization_id)]."""
    out = []
    for _ in range(n):
        db, agent, biz, voice = _fresh_agent(None)
        agent.retell_agent_id, agent.retell_llm_id = (
            f"old_{uuid.uuid4().hex[:6]}",
            f"oldllm_{uuid.uuid4().hex[:6]}",
        )
        agent.greeting = "Our old greeting."
        agent.synced_version = agent.version
        db.commit()
        out.append((agent.id, agent.organization_id))
        db.close()
    return out


def _snapshot():
    db = SessionLocal()
    try:
        return sorted(
            (
                str(a.id),
                a.version,
                a.synced_version,
                a.retell_agent_id,
                a.retell_llm_id,
                a.closed_retell_agent_id,
                a.greeting,
            )
            for a in db.query(Agent).all()
        )
    finally:
        db.close()


def _run(prov, **kw):
    db = SessionLocal()
    try:
        return resync_agents(db, prov, **kw)
    finally:
        db.close()


def test_dry_run_is_the_default_makes_zero_provider_calls_and_writes_nothing():
    _existing_agents()
    prov = RecordingAgents()
    for kw in ({}, {"organization_id": _existing_agents(1)[0][1]}):
        before = _snapshot()
        r = _run(prov, **kw)
        assert r["applied"] is False and r["synced"] == 0 and r["agents"] >= 1
        assert _snapshot() == before
    assert prov.created == [] and prov.updated == []


def test_dry_run_lists_exactly_the_agents_it_would_sync():
    (a1, o1), (a2, o2), (a3, o3) = _existing_agents()
    everything = {t["agent_id"] for t in _run(RecordingAgents())["targets"]}
    assert {str(a1), str(a2), str(a3)} <= everything
    one = _run(RecordingAgents(), organization_id=o2)
    assert one["scope"] == str(o2) and one["agents"] == 1
    assert one["targets"] == [{"agent_id": str(a2), "organization_id": str(o2)}]


def test_apply_without_an_explicit_scope_is_refused_and_touches_nothing():
    _existing_agents()
    before, prov = _snapshot(), RecordingAgents()
    with pytest.raises(ScopeRequired):
        _run(prov, apply=True)
    assert prov.created == [] and prov.updated == [] and _snapshot() == before


def test_apply_to_one_organization_syncs_only_that_organization():
    (a1, o1), (a2, o2), (a3, o3) = _existing_agents()
    prov = RecordingAgents()
    r = _run(prov, apply=True, organization_id=o2)
    assert (r["agents"], r["synced"], r["failed"]) == (1, 1, [])
    pushed = {u[1] for u in prov.updated if u[0] == "agent"}
    db = SessionLocal()
    try:
        ids = {
            x.id: x.retell_agent_id
            for x in db.query(Agent).filter(Agent.id.in_([a1, a2, a3]))
        }
    finally:
        db.close()
    assert pushed == {
        ids[a2]
    }  # the other two organizations' agents were never sent to Retell
    assert all(
        c.begin_message.startswith(opening_disclosure("Pipe Biz"))
        for k, _i, c in prov.updated
        if k == "agent"
    )


def test_apply_all_orgs_is_the_explicit_way_to_sync_everyone():
    existing = _existing_agents()
    prov = RecordingAgents()
    r = _run(prov, apply=True, all_organizations=True)
    mine = {str(a) for a, _o in existing}
    assert r["scope"] == "all organizations" and mine <= {
        t["agent_id"] for t in r["targets"]
    }
    assert not mine & set(
        r["failed"]
    )  # (the database may hold other tests' leftovers; only our agents are asserted)
    db = SessionLocal()
    try:
        ids = {
            x.retell_agent_id
            for x in db.query(Agent).filter(Agent.id.in_([a for a, _o in existing]))
        }
    finally:
        db.close()
    assert ids <= {
        u[1] for u in prov.updated if u[0] == "agent"
    }  # every organization's agent was pushed


def test_one_agents_failure_does_not_stop_the_next_agents():
    existing = _existing_agents(3)
    db = SessionLocal()
    try:
        rows = {
            x.id: (x.retell_agent_id, x.retell_llm_id)
            for x in db.query(Agent).filter(Agent.id.in_([a for a, _o in existing]))
        }
    finally:
        db.close()
    victim = existing[1][
        0
    ]  # a middle one, so agents both before and after it exist in any processing order

    class FailsOne(RecordingAgents):
        def update_llm(self, llm_id, config):
            if llm_id == rows[victim][1]:
                raise RuntimeError("provider down")
            super().update_llm(llm_id, config)

    prov = FailsOne()
    r = _run(prov, apply=True, all_organizations=True)
    mine = {str(a) for a, _o in existing}
    assert str(victim) in r["failed"] and set(r["failed"]) & mine == {str(victim)}
    pushed = {u[1] for u in prov.updated if u[0] == "agent"}
    assert rows[victim][0] not in pushed
    assert all(
        rows[a][0] in pushed for a, _o in existing if a != victim
    )  # everyone else was still processed


def test_the_command_line_refuses_apply_without_scope_and_a_dry_run_prints_agent_ids(
    monkeypatch, capsys
):
    from app.workers import resync_agents as mod

    (a1, o1), *_ = _existing_agents(2)
    monkeypatch.setattr("sys.argv", ["resync_agents", "--apply"])
    with pytest.raises(SystemExit) as e:
        mod.main()
    assert e.value.code != 0
    monkeypatch.setattr("sys.argv", ["resync_agents", "--org", str(o1)])
    mod.main()
    out = capsys.readouterr().out
    assert (
        "DRY RUN" in out
        and f"agent {a1}" in out
        and f"organization {o1}" in out
        and "nothing changed" in out
    )
