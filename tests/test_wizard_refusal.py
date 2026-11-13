"""Refusing a shared NAME: Back, another broker, redo, DONE and VERIFY after a restart, and the receipt."""
from __future__ import annotations

import json
import re

import pytest
import yaml

from fakes import K1, K2, K3, ScriptedIO, ScriptExhausted, assert_no_leak, env_finding
from test_wizard_consent import COMMUNITY_ROUTE, OR_FINDING, ORKEY, SHARED_CATALOG
from test_wizard_e2e import LOCAL_SHARE, deps_for, dotenv_read, world  # noqa: F401 - world is a fixture
from test_wizard_flow import LOCAL_ROUTE, TOP, entry, mk  # noqa: F401
from trading_agent.profile.hermes_cli import HermesUnavailable
from trading_agent.profile.receipt import Receipt
from trading_agent.profile.writer import AiChoice, WriteError
from trading_agent.wizard import screens
from trading_agent.wizard.flow import _Wizard, run_wizard
from trading_agent.wizard.state import State

TWO_BROKERS = [entry("kraken", "Third Party", [COMMUNITY_ROUTE]), entry("alpaca", "Alpaca", [LOCAL_ROUTE])]
TO_REFUSAL = [*TOP, "paste-openrouter", K1, "continue", "pick:kraken", "continue", "refuse"]


def run_to(mk, answers, **kw):
    w = mk(findings=[], environ={}, catalog=kw.pop("catalog", TWO_BROKERS), **kw)
    io = ScriptedIO(answers)
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    return w, io


# ------------------------------------------------------------------ I-1: Back never crashes after a refusal

@pytest.mark.parametrize("label,more,expect", [
    ("SKILLS back", ["back"], "Connect Third Party"),
    ("REVIEW back", ["done", "back", "back"], "Connect Third Party"),
    ("DATA_KEYS back", ["lib:skill-keyed", "done", "back", "back"], "Connect Third Party"),
])
def test_back_after_a_refusal_returns_to_the_install_screen(mk, label, more, expect):
    w, io = run_to(mk, [*TO_REFUSAL, *more])
    assert expect in io.prompts[-1][1], label
    assert io.option_ids() == ["continue", "redo", "back"]             # redo is reachable for a refused broker


def test_the_refusal_stays_until_the_user_chooses_again(mk):
    w = mk(findings=[], environ={}, catalog=TWO_BROKERS)
    receipt, io = w.run([*TO_REFUSAL, "back", "continue", "done", "do"])   # SKILLS back, Continue (no new question)
    assert w.writes[0].plan.broker is None and "confirm" not in io.kinds()
    assert sum("will be able to read" in p for k, p, _ in io.prompts if k == "choose") == 1


def test_redo_asks_about_the_refused_broker_again(mk):
    w = mk(findings=[], environ={}, catalog=TWO_BROKERS)
    answers = [*TO_REFUSAL, "back", "redo", "same", "done", "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].plan.broker is not None and dict(w.writes[0].values) == {ORKEY: K1}


def test_refuse_then_pick_another_broker_leaves_only_the_new_one(mk):
    w = mk(findings=[], environ={}, catalog=TWO_BROKERS)
    answers = [*TO_REFUSAL, "back", "back", "pick:alpaca", "continue", "skip", "skip", "", "done", "do"]
    receipt, io = w.run(answers)
    plan = w.writes[0].plan
    assert plan.broker.slug == "alpaca" and dict(w.writes[0].values) == {ORKEY: K1}
    review = next(p for k, p, _ in io.prompts if "exactly what I will do" in p)
    assert "left out" not in review and "Third Party" not in review
    decisions = json.loads(w.state_text())["decisions"]
    assert not [k for k in decisions if "kraken" in k]                  # the refused broker left no trace


def test_e2e_refuse_then_another_broker_writes_only_the_new_one(world):
    deps = deps_for(world, catalog=TWO_BROKERS)
    answers = [*TO_REFUSAL, "back", "back", "pick:alpaca", "continue", "skip", "skip", "", "done", "do"]
    receipt = run_wizard(ScriptedIO(answers), deps)
    cfg = yaml.safe_load((world.pdir() / "config.yaml").read_text())
    assert list(cfg["mcp_servers"]) == ["alpaca"] and "some-community-mcp" not in json.dumps(cfg)
    assert dotenv_read(world.pdir() / ".env") == {ORKEY: K1}
    assert receipt.broker == {"slug": "alpaca", "route_id": "local"} and receipt.env_names_written == [ORKEY]


# ------------------------------------------------------------------ M-a, M-b, M-c (R5b, R7): DONE and VERIFY tell the truth

def refuse_run(world, answers):
    deps = deps_for(world, findings=[env_finding(ORKEY)], catalog=[entry("kraken", "Third Party", [LOCAL_SHARE])])
    world.platform._e[ORKEY] = K1
    io = ScriptedIO(answers)
    return run_wizard(io, deps), io, deps


FORWARD = [*TOP, f"use:env:{ORKEY}", True, "continue", "pick:kraken", "continue", "refuse", "done", "do"]


def test_done_screen_itself_lists_the_left_out_consumer_and_every_reader(world):
    receipt, io, _ = refuse_run(world, FORWARD)
    done = io.said[-1]
    assert done.startswith("You are set up.") and "AI: OpenRouter." in done and "Broker: none" in done
    assert "the Third Party connection (it is left out" in done and "you skipped it" not in done
    assert "OPENROUTER_API_KEY: the OpenRouter AI connection." in done
    assert receipt.broker is None and receipt.env_names_written == [ORKEY]           # R7: the receipt tells the truth
    assert receipt.ai_choice == {"kind": "openrouter", "ref": ""} and receipt.skills == []


def test_done_with_a_granted_broker_names_every_reader_and_the_destination(world):
    answers = [*FORWARD[:-3], "same", "done", "do"]
    receipt, io, _ = refuse_run(world, answers)
    done = io.said[-1]
    assert "The Third Party connection runs uvx some-community-mcp." in done
    assert "OPENROUTER_API_KEY: the OpenRouter AI connection and the Third Party connection" in done
    assert receipt.broker == {"slug": "kraken", "route_id": "local"}


def test_reverse_refusal_done_never_says_ai_openrouter(world):
    deps = deps_for(world, catalog=[entry("kraken", "Third Party", [LOCAL_SHARE])])
    io = ScriptedIO([*TOP, "paste-openrouter", "", "continue", "pick:kraken", "continue", "paste", K2, "done",
                     "refuse", "do"])
    receipt = run_wizard(io, deps)
    done = io.said[-1]
    assert "AI: no AI yet" in done and "AI: OpenRouter" not in done and "you skipped it" not in done
    assert "the OpenRouter AI connection (it is left out" in done
    assert receipt.ai_choice == {"kind": "none", "ref": ""} and receipt.broker == {"slug": "kraken", "route_id": "local"}


@pytest.mark.parametrize("reverse", [False, True], ids=["broker-refused", "ai-refused"])
def test_a_restart_at_verify_shows_exactly_what_was_installed(world, reverse):
    if reverse:
        deps = deps_for(world, catalog=[entry("kraken", "Third Party", [LOCAL_SHARE])])
        answers = [*TOP, "paste-openrouter", "", "continue", "pick:kraken", "continue", "paste", K2, "done", "refuse", "do"]
        receipt = run_wizard(ScriptedIO(answers), deps)
    else:
        receipt, _, deps = refuse_run(world, FORWARD)
    path = world.config_home / "trading-agent" / "state.json"
    doc = json.loads(path.read_text())
    doc["current"] = "VERIFY"                                   # interrupted after the write, before the end
    path.write_text(json.dumps(doc))
    io = ScriptedIO([])
    again = run_wizard(io, deps)
    said = "\n".join(io.said)
    assert again == receipt
    if reverse:
        assert "AI check" not in said and "AI: no AI yet" in said and "the OpenRouter AI connection (it is left out" in said
        assert "Broker connection: not tested yet" in said
    else:
        assert "Broker connection" not in said and "Broker: none" in said and "Test the Third Party" not in said
        assert "the Third Party connection (it is left out" in said and "AI: OpenRouter." in said


# ------------------------------------------------------------------ M-e: nothing to verify for a consumer that is left out

def test_a_left_out_broker_gets_no_verify_message(mk):
    w = mk(findings=[], environ={}, catalog=TWO_BROKERS)
    receipt, io = w.run([*TO_REFUSAL, "done", "do"])
    assert "Testing the Third Party connection" not in "\n".join(io.said)


def test_a_left_out_ai_is_not_offered_a_test(mk):
    answers = [*TOP, "paste-openrouter", "", "continue", "pick:kraken", "continue", "paste", K2, "done", "refuse",
               "back", "back", "back", "back"]            # REVIEW, SKILLS, install screen, broker menu, AI screen
    w, io = run_to(mk, answers)
    assert "Nothing to test" in io.prompts[-1][1] and "Check that it answers" not in io.everything()


# ------------------------------------------------------------------ M-f: who gave the value, and a different value

def test_redo_attributes_the_value_to_the_consumer_that_gave_it(mk):
    w = mk(findings=[], environ={}, catalog=TWO_BROKERS)
    answers = [*TOP, "paste-openrouter", "", "continue", "pick:kraken", "continue", "paste", K2, "done", "same",
               "back", "back", "redo", "paste", K3, "done", "same", "do"]
    receipt, io = w.run(answers)
    asks = [p for k, p, _ in io.prompts if k == "choose" and "will be able to read" in p]
    assert len(asks) == 2
    for p in asks:
        assert f"the {ORKEY} you gave the Third Party connection" in p        # never "you gave the OpenRouter AI connection"
    assert dict(w.writes[0].values) == {ORKEY: K3}


def test_a_different_value_can_be_entered_and_the_other_consumer_is_asked_about_it(mk):
    w = mk(findings=[OR_FINDING], environ={ORKEY: K1}, catalog=SHARED_CATALOG)
    answers = [*TOP, f"use:env:{ORKEY}", True, "continue", "pick:kraken", "continue", "different", K2, "done", "same", "do"]
    receipt, io = w.run(answers)
    assert dict(w.writes[0].values) == {ORKEY: K2}                         # the new value, for both, with both asked
    asks = [p for k, p, _ in io.prompts if k == "choose" and "will be able to read" in p]
    assert asks[0].startswith("The Community Broker connection will be able to read") and \
        asks[1].startswith("The OpenRouter AI connection will be able to read") and "you gave the Community Broker" in asks[1]
    assert [o for o in io.prompts if o[1] == asks[0]][0][2][1][0] == "different"
    assert_no_leak(io.everything() + w.state_text(), K1, K2)


# ------------------------------------------------------------------ M-c: switching the AI, and the final plan

def test_switching_the_ai_address_asks_for_the_key_again(mk):
    w = mk()
    answers = [*TOP, "custom", "https://a.example.com/v1", "MY_KEY", K1, "back", "custom", "https://b.example.org/v1",
               "MY_KEY", K2, "continue", "none", "done", "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].plan.ai == AiChoice("custom", "https://b.example.org/v1", "MY_KEY")
    assert dict(w.writes[0].values) == {"MY_KEY": K2}                      # the key for a.example.com did not follow
    assert sum(1 for k, p, _ in io.prompts if k == "secret") == 2
    assert_no_leak(io.everything(), K1)


def test_resume_with_a_changed_route_that_adds_a_name_asks_about_the_new_name(mk):
    w = mk(findings=[], environ={}, catalog=[entry("alpaca", "Alpaca", [LOCAL_ROUTE])])
    answers = [*TOP, "skip", "continue", "pick:alpaca", "continue", "skip", "skip", "", "done"]
    with pytest.raises(ScriptExhausted):
        w.run(answers)
    more = dict(LOCAL_ROUTE, auth={"type": "x", "env_vars": [
        *LOCAL_ROUTE["auth"]["env_vars"], {"name": "NEW_NAME_KEY", "role": "secret", "secret": True}]})
    w2 = mk(findings=[], environ={}, catalog=[entry("alpaca", "Alpaca", [more])])
    w2.state_path().write_text(w.state_text())
    io = ScriptedIO(["skip", "skip", "", "skip", "do"])
    receipt = run_wizard(io, w2.deps)
    assert screens.STALE_NOTE in io.said
    assert any("NEW_NAME_KEY" in p for k, p, _ in io.prompts if k == "choose")


def test_the_final_plan_resolution_drops_a_consumer_added_after_the_grants_were_computed(mk):
    w = mk(findings=[], environ={}, catalog=SHARED_CATALOG)
    io = ScriptedIO(["refuse"])
    wiz = _Wizard(io, w.deps)
    wiz.decide("profile_name", "trader")
    wiz.decide("ai.kind", "openrouter")
    wiz._values[ORKEY] = K1                                    # grants computed for the AI alone...
    wiz._grants[("ai", ORKEY)] = "paste"
    wiz._labels["ai"] = "the OpenRouter AI connection"
    wiz.decide("broker.slug", "kraken")                         # ...then a consumer of the same NAME appears
    wiz.decide("broker.route", "community")
    assert wiz._st_write() is State.REVIEW                       # asked at WRITE time: back to a fresh REVIEW, no write
    assert w.writes == []
    plan = wiz._build_plan()
    assert plan.broker is None and dict(plan.env_values) == {ORKEY: K1} and plan.ai == AiChoice("openrouter")
    assert io.prompts and "will be able to read" in io.prompts[0][1]


# ------------------------------------------------------------------ M-g: no internal state names in what the user sees

STATE_NAMES = re.compile(r"\b(START|PRECHECK|SCAN|AI_ROUTE|AI_VERIFY|BROKER_PICK|BROKER_INSTALL|BROKER_VERIFY|SKILLS|"
                         r"DATA_KEYS|REVIEW|WRITE|VERIFY|DONE)\b")


def test_no_screen_constant_mentions_an_internal_state_name():
    for name in (n for n in dir(screens) if not n.startswith("_")):
        value = getattr(screens, name)
        texts = value.values() if isinstance(value, dict) else [value]
        for t in texts:
            if isinstance(t, str):
                assert not STATE_NAMES.search(t), f"{name} mentions a state name"


def test_no_user_visible_output_mentions_a_state_name(mk, world):
    outs = []
    w = mk(findings=[], environ={}, catalog=TWO_BROKERS, hermes_kw={"raises": HermesUnavailable("x")})
    outs.append(w.run(["start", "stop"])[1])
    w2 = mk(findings=[], environ={}, catalog=TWO_BROKERS)
    w2.state_path().unlink(missing_ok=True)
    w2.write_error = WriteError("x", Receipt(profile_name="trader"))
    outs.append(w2.run([*TO_REFUSAL, "done", "do", "stop"])[1])
    for io in outs:
        assert not STATE_NAMES.search(io.everything())


# ------------------------------------------------------------------ N1 gap: _validate_decisions on forged left: records

FORGED_LEFT = {
    "left:ai": "‮" + "A" * 10,                      # bidi override in the value
    "left:broker:kraken/community": "A" * 512,            # 512-char value
    "left:evil": ORKEY,                                    # unknown key shape
    "left:lib:x": "lower_case_name",                      # lower-case value
    "left:lib:y": "⁦FOO⁩",                      # bidi isolate characters
}


def test_validate_decisions_drops_every_forged_left_record(mk):
    w = mk(findings=[], environ={}, catalog=TWO_BROKERS)
    wiz = _Wizard(ScriptedIO([]), w.deps)
    wiz.ws.decisions.update(FORGED_LEFT)
    keep = {"left:broker:kraken/community2": ORKEY}       # a well-formed record survives
    wiz.ws.decisions.update(keep)
    wiz._validate_decisions()
    left = {k: v for k, v in wiz.ws.decisions.items() if k.startswith("left:")}
    assert left == keep


def test_resume_at_verify_never_prints_a_forged_left_record(world):
    deps = deps_for(world, findings=[env_finding(ORKEY)], catalog=[entry("kraken", "Third Party", [LOCAL_SHARE])])
    world.platform._e[ORKEY] = K1
    answers = [*TOP, f"use:env:{ORKEY}", True, "continue", "pick:kraken", "continue", "same", "done", "do"]
    receipt = run_wizard(ScriptedIO(answers), deps)
    assert receipt is not None and world.receipt_path().exists()   # a trusted receipt is on disk
    path = world.config_home / "trading-agent" / "state.json"
    doc = json.loads(path.read_text())
    doc["current"] = "VERIFY"
    doc["decisions"].update(FORGED_LEFT)
    path.write_text(json.dumps(doc))
    io = ScriptedIO([])
    assert run_wizard(io, deps) == receipt
    said = "\n".join(io.said)
    assert "‮" not in said and "⁦" not in said and "A" * 64 not in said and "lower_case_name" not in said
    assert "left out" not in said.lower()
