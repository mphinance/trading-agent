"""Consent per consumer and per process, resume safety, hostile saved state, input rules, honest copy."""
from __future__ import annotations

import json
import traceback

import pytest

from fakes import (
    assert_state_is_clean,
    ALL_SECRETS, K1, K2, K3, K4, K5, FakeStore, ScriptedIO, ScriptExhausted, assert_no_leak, cli_finding,
    env_finding, finding, port_finding, sweep_tree,
)
from test_wizard_flow import (  # noqa: F401 - mk is a fixture
    ALPACA_ENV, ALPACA_FINDINGS, AI_OLLAMA, LOCAL_ROUTE, OLLAMA, TOP, World, entry, lib, mk,
)
from trading_agent.profile.writer import AiChoice
from trading_agent.wizard import screens
from trading_agent.wizard.flow import Deps, run_wizard
from trading_agent.wizard.state import State, WizardState

ORKEY = "OPENROUTER_API_KEY"
OR_FINDING = env_finding(ORKEY)
# a third-party route that happens to list the AI key's name
COMMUNITY_ROUTE = {
    "route_id": "community", "kind": "local-command", "maintainer": "community",
    "install": {"method": "uvx", "command_text": "uvx some-community-mcp"},
    "auth": {"type": "api-key", "env_vars": [{"name": ORKEY, "role": "key-id", "required": True, "secret": True}]},
}
SHARED_CATALOG = [entry("kraken", "Community Broker", [COMMUNITY_ROUTE])]


# ------------------------------------------------------------------ C1: consent is per consumer

def run_shared(mk, answers, *, catalog=None, libraries=None, environ=None, findings=None):
    w = mk(environ=environ if environ is not None else {ORKEY: K1}, findings=findings if findings is not None else [OR_FINDING],
           catalog=catalog or SHARED_CATALOG, libraries=libraries)
    receipt, io = w.run(answers)
    review = next(p for k, p, _ in io.prompts if "Here is exactly what I will do" in p)
    return w, io, w.writes[0].plan, review


AI_THEN_BROKER = [*TOP, f"use:env:{ORKEY}", True, "continue", "pick:kraken", "continue"]


def test_refusing_to_share_a_name_removes_the_broker_from_the_plan_entirely(mk):
    w, io, plan, review = run_shared(mk, [*AI_THEN_BROKER, "refuse", "done", "do"])
    share = next(p for k, p, o in io.prompts if k == "choose" and "will be able to read" in p)
    assert "The Community Broker connection will be able to read the OPENROUTER_API_KEY you gave the OpenRouter AI connection" in share
    assert plan.broker is None                                            # not installed: no entry, no reference
    assert plan.ai == AiChoice("openrouter") and dict(w.writes[0].values) == {ORKEY: K1}
    assert "left out" in review.lower() and "Community Broker" in review
    assert_no_leak(io.everything() + w.state_text(), K1)


def test_granting_the_same_value_lists_every_reader_in_review(mk):
    w, io, plan, review = run_shared(mk, [*AI_THEN_BROKER, "same", "done", "do"])
    assert plan.broker is not None and dict(w.writes[0].values) == {ORKEY: K1}
    assert f"{ORKEY}: the OpenRouter AI connection and the Community Broker connection" in review
    assert "(no key yet)" not in review


def test_reverse_ai_skipped_then_broker_pastes_the_same_name_asks_the_ai_consumer(mk):
    answers = [*TOP, "paste-openrouter", "", "continue", "pick:kraken", "continue", "paste", K2, "done",
               "same", "do"]
    w, io, plan, review = run_shared(mk, answers, findings=[], environ={})
    assert any("OpenRouter AI connection will be able to read the OPENROUTER_API_KEY you gave the Community Broker" in p
               for k, p, _ in io.prompts if k == "choose")
    assert plan.ai == AiChoice("openrouter") and dict(w.writes[0].values) == {ORKEY: K2}
    assert "(no key yet)" not in review and f"{ORKEY}: the OpenRouter AI connection and the Community Broker" in review
    done = "\n".join(io.said)
    assert "you skipped it" not in done                                    # the false finish-later line is gone


def test_reverse_refusal_leaves_the_ai_unconfigured_and_says_so(mk):
    answers = [*TOP, "paste-openrouter", "", "continue", "pick:kraken", "continue", "paste", K2, "done",
               "refuse", "do"]
    w, io, plan, review = run_shared(mk, answers, findings=[], environ={})
    assert plan.ai == AiChoice("none") and plan.broker is not None and dict(w.writes[0].values) == {ORKEY: K2}
    assert "(no key yet)" not in review and "left out" in review.lower() and "OpenRouter" in review
    done = "\n".join(io.said)
    assert "you skipped it" not in done and "left out" in done.lower()


def test_a_library_key_needs_its_own_grant_in_both_directions(mk):
    libs = [lib("needs-or", needs=(ORKEY,))]
    data = finding("env2:" + ORKEY, "data-key", "a data key", [ORKEY])
    answers = [*TOP, f"use:env:{ORKEY}", True, "continue", "none", "lib:needs-or", "done", "add", "refuse", "do"]
    w = mk(environ={ORKEY: K1}, findings=[OR_FINDING, data], libraries=libs)
    receipt, io = w.run(answers)
    assert "Library needs-or" in next(p for k, p, o in io.prompts if k == "choose" and "will be able to read" in p)
    assert w.writes[0].plan.skills == [] and w.writes[0].plan.ai == AiChoice("openrouter")
    lines = "\n".join(io.said).splitlines()
    done = lines[lines.index("You are set up."):]
    who = done.index(screens.DONE_KEYS)
    assert done[who + 1] == f"  {ORKEY}: the OpenRouter AI connection."     # the refused library cannot read it
    assert not any("needs-or" in line for line in done[who:who + 2])
    assert any("Library needs-or library (it is left out" in line and ORKEY in line for line in done)
    w2 = mk(environ={ORKEY: K1}, findings=[OR_FINDING, data], libraries=libs)
    w2.state_path().unlink()
    receipt, io2 = w2.run([*answers[:-2], "same", "do"])
    assert w2.writes[0].plan.skills == ["needs-or"]


def test_custom_ai_consumer_is_named_by_its_host_with_no_grammar_slip(mk):
    answers = [*TOP, "custom", "https://llm.example.org/v1", ORKEY, K1, "continue", "pick:kraken", "continue", "refuse",
               "done", "do"]
    w, io, plan, review = run_shared(mk, answers, findings=[], environ={})
    text = io.everything()
    assert "the AI service at llm.example.org" in text and "the an " not in text
    assert plan.broker is None


def test_reuse_prompt_names_the_consumer_in_plain_words(mk):
    w = mk(environ=ALPACA_ENV, findings=[*OLLAMA, *ALPACA_FINDINGS])
    receipt, io = w.run([*TOP, *AI_OLLAMA, "pick:alpaca", "continue", True, True, "true", "done", "do"])
    for p in (p for k, p, _ in io.prompts if k == "confirm"):
        assert "so the Alpaca connection can use it" in p


# ------------------------------------------------------------------ C2: nothing carries over a restart

def interrupted_at_review(w, environ_value=K1):
    answers = [*TOP, f"use:env:{ORKEY}", True, "continue", "none", "done"]
    with pytest.raises(ScriptExhausted):
        w.run(answers)
    assert WizardState.load(w.platform).current is State.REVIEW


def test_no_reuse_decision_is_ever_saved(mk):
    w = mk(environ={ORKEY: K1}, findings=[OR_FINDING])
    interrupted_at_review(w)
    assert not [k for k in json.loads(w.state_text())["decisions"] if k.startswith(("ai:", "broker:", "lib:"))]
    sweep_tree(w.tmp, [K1])


def test_after_a_restart_the_same_consent_prompt_is_asked_again(mk):
    w = mk(environ={ORKEY: K1}, findings=[OR_FINDING])
    interrupted_at_review(w)
    w.platform._environ[ORKEY] = K4                       # the value changed meanwhile
    receipt, io = w.run([True, "do"])
    assert io.kinds()[0] == "confirm" and "OpenRouter AI connection" in io.prompts[0][1]
    assert w.writes[0].values == {ORKEY: K4}
    w2 = mk(environ={ORKEY: K1}, findings=[OR_FINDING])
    w2.state_path().unlink()
    interrupted_at_review(w2)
    receipt, io2 = w2.run([False, "skip", "do"])           # no: nothing is copied; the choice is offered again
    assert w2.writes[0].values == {}


def test_after_a_restart_a_vanished_finding_is_never_copied(mk):
    w = mk(environ={ORKEY: K1}, findings=[OR_FINDING])
    interrupted_at_review(w)
    w2 = mk(environ={ORKEY: K1}, findings=[])             # same environ value, but no live finding any more
    receipt, io = w2.run(["skip", "do"])
    assert "confirm" not in io.kinds() and w2.writes[0].values == {}


def test_after_a_restart_a_login_shell_only_finding_cannot_be_copied(mk):
    w = mk(environ={ORKEY: K1}, findings=[OR_FINDING])
    interrupted_at_review(w)
    shell = env_finding(ORKEY, where="login shell environment")
    w2 = mk(environ={ORKEY: K1}, findings=[shell])
    receipt, io = w2.run(["skip", "do"])
    assert "confirm" not in io.kinds() and w2.writes[0].values == {}


def test_a_declined_finding_stays_declined_across_a_restart(mk):
    w = mk(environ={ORKEY: K1}, findings=[OR_FINDING])
    with pytest.raises(ScriptExhausted):
        w.run([*TOP, f"use:env:{ORKEY}", False])
    assert json.loads(w.state_text())["decisions"][f"declined:env:{ORKEY}"] == "1"


def test_hostile_state_cannot_make_the_wizard_copy_an_unrelated_environment_value(mk):
    w = mk(environ={"AWS_SECRET_ACCESS_KEY": K4}, findings=[])
    ws = WizardState(State.REVIEW, {"profile_name": "trader", "ai.kind": "custom", "ai.ref": "https://evil.example/v1",
                                    "ai.key_env": "AWS_SECRET_ACCESS_KEY", "skills.done": "1"})
    ws.save(w.platform)
    receipt, io = w.run(["skip", "do"])
    assert "confirm" not in io.kinds() and w.writes[0].values == {}
    assert_no_leak(io.everything(), K4)


def test_a_changed_route_on_resume_drops_its_value_decisions_and_asks_again(mk):
    w = mk(findings=OLLAMA)
    answers = [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", "skip", "skip", "", "done"]
    with pytest.raises(ScriptExhausted):
        w.run(answers)                                    # skips are persisted
    assert any(k.startswith("broker:alpaca/local:") for k in json.loads(w.state_text())["decisions"])
    changed = dict(LOCAL_ROUTE, install={"method": "uvx", "command_text": "uvx something-else"})
    w2 = mk(findings=OLLAMA, catalog=[entry("alpaca", "Alpaca", [changed])])
    w2.state_path().write_text(w.state_text())
    io = ScriptedIO([])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w2.deps)
    assert screens.STALE_NOTE in io.said
    assert io.kinds()[0] == "choose" and "ALPACA_API_KEY" in io.prompts[0][1]      # asked again, not remembered


def test_review_shows_where_a_key_will_go(mk):
    w = mk(environ=ALPACA_ENV, findings=[*OLLAMA, *ALPACA_FINDINGS])
    receipt, io = w.run([*TOP, *AI_OLLAMA, "pick:alpaca", "continue", True, True, "true", "done", "do"])
    review = next(p for k, p, _ in io.prompts if "Here is exactly what I will do" in p)
    assert "runs uvx alpaca-mcp-server" in review
    w2 = mk(findings=OLLAMA)
    w2.state_path().unlink()
    receipt, io2 = w2.run([*TOP, *AI_OLLAMA, "pick:tradier", "continue", "true", "skip", "done", "do"])
    assert "connects to mcp.tradier.com/mcp" in next(p for k, p, _ in io2.prompts if "exactly what I will do" in p)


# ------------------------------------------------------------------ I3: Hermes version rules

@pytest.mark.parametrize("version", ["Python 3.12.4; hermes 0.21.0", "Hermes Agent v0.23.0rc1", "Hermes Agent v0.22.9"])
def test_old_and_prerelease_versions_block_with_the_update_text(mk, version):
    w = mk(hermes_kw={"version": version})
    receipt, io = w.run(["start", "stop"])
    assert receipt is None and "hermes update" in "\n".join(io.said) and io.option_ids() == ["again", "stop", "back"]


@pytest.mark.parametrize("version", ["Hermes Agent v0.23.1 (2026.9.24)", "Hermes Agent v0.23.0+canary.20261012", "hermes 0.24.0"])
def test_good_versions_pass(mk, version):
    w = mk(hermes_kw={"version": version})
    io = ScriptedIO(["start"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert io.kinds()[-1] == "ask"


def test_unreadable_version_asks_before_continuing(mk):
    w = mk(hermes_kw={"version": "total garbage"})
    io = ScriptedIO(["start"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert io.option_ids() == ["continue", "again", "stop", "back"]


# ------------------------------------------------------------------ I5, I8: inputs

def test_custom_address_rules_and_no_crash_on_a_long_or_bidi_address(mk):
    w = mk()
    bad = ["http://example.com/v1", "https://" + "a" * 600 + ".test/v1", "https://api.test/v1‮", "https://api.test/é"]
    answers = [*TOP, "custom", *bad, "http://localhost:9000/v1", "", "continue", "none", "done", "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].plan.ai == AiChoice("custom", "http://localhost:9000/v1", "")
    assert io.said.count(screens.AI_ADDRESS_INVALID) == 4
    for b in bad:
        assert b not in io.everything()


def test_back_on_every_ask_prompt(mk):
    # custom address: back to the AI menu, nothing stored
    w = mk()
    io0 = ScriptedIO([*TOP, "custom", "<"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io0, w.deps)
    assert "brain" in io0.prompts[-1][1] and io0.kinds()[-1] == "choose"
    assert not [k for k in json.loads(w.state_text())["decisions"] if k.startswith("ai.")]
    # custom key name: back to the AI menu, nothing recorded
    w2 = mk()
    w2.state_path().unlink()
    io = ScriptedIO([*TOP, "custom", "http://localhost:9000/v1", "<"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w2.deps)
    assert "brain" in io.prompts[-1][1] and "ai.kind" not in json.loads(w2.state_text())["decisions"]
    # plain mode switch: back to the broker menu, not stored as a value
    w3 = mk(findings=OLLAMA)
    w3.state_path().unlink()
    io3 = ScriptedIO([*TOP, *AI_OLLAMA, "pick:alpaca", "continue", "skip", "skip", "<"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io3, w3.deps)
    assert "broker MCP" in io3.prompts[-1][1]
    # search prompts
    w4 = mk(findings=OLLAMA)
    w4.state_path().unlink()
    io4 = ScriptedIO([*TOP, *AI_OLLAMA, "search", "<"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io4, w4.deps)
    assert "broker MCP" in io4.prompts[-1][1]


def test_route_picker_back_returns_to_the_broker_menu(mk):
    two = entry("etoro", "Two", [dict(LOCAL_ROUTE, route_id="a"), dict(LOCAL_ROUTE, route_id="b")])
    w = mk(findings=OLLAMA, catalog=[two])
    io = ScriptedIO([*TOP, *AI_OLLAMA, "pick:etoro", "back"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert "broker MCP" in io.prompts[-1][1]


def test_unknown_menu_choice_is_a_replay_not_a_crash(mk):
    class Loose(ScriptedIO):
        def choose(self, prompt, options):
            return self._next("choose", prompt, options)
    w = mk()
    io = Loose([*TOP, "bogus", "skip", "continue", "none", "done", "do"])
    assert run_wizard(io, w.deps) is not None
    assert screens.INVALID_CHOICE in io.said


# ------------------------------------------------------------------ I6: hostile state and catalog

def hostile(w, **decisions):
    WizardState(State.AI_VERIFY, decisions).save(w.platform)


@pytest.mark.parametrize("decisions", [
    {"profile_name": "../x"}, {"profile_name": "root"}, {"ai.kind": "bogus"},
    {"ai.kind": "custom", "ai.ref": "http://evil.example/v1"}, {"ai.kind": "custom", "ai.ref": "https://ok.test/v1", "ai.key_env": "lower"},
    {"broker.slug": "ghost", "broker.route": "x"}, {"skills": "ghost-lib"}, {"scan.apps": "maybe"},
])
def test_invalid_saved_answers_are_dropped_with_a_plain_note(mk, decisions):
    w = mk()
    hostile(w, **decisions)
    io = ScriptedIO([])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert screens.STATE_DROPPED in io.said
    saved = WizardState.load(w.platform).decisions
    for k, v in decisions.items():
        assert saved.get(k) != v or k == "skills" and saved.get(k) == ""


def test_write_state_without_a_profile_name_restarts_at_the_name(mk):
    w = mk()
    WizardState(State.WRITE, {"ai.kind": "none"}).save(w.platform)
    seen, hook = w.trace()
    with pytest.raises(ScriptExhausted):
        w.run([], on_prompt=hook)
    assert seen[-1] is State.PRECHECK


def test_deeply_nested_state_file_loads_as_fresh(mk):
    w = mk()
    w.state_path().parent.mkdir(parents=True, exist_ok=True)
    w.state_path().write_text("[" * 200000)
    assert WizardState.load(w.platform).current is State.START


def test_an_unwritable_config_dir_says_so_once_and_setup_still_finishes(mk, tmp_path):
    w = mk()
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    w.platform._config_home = blocker / "cfg"            # mkdir under a file fails with OSError
    receipt, io = w.run([*TOP, "skip", "continue", "none", "done", "do"])
    assert receipt is not None and io.said.count(screens.STATE_NOT_SAVED) == 1


@pytest.mark.parametrize("bad_name", ["bad-name!", "lower_case", "X" * 65, "9LEADS"])
def test_a_route_with_an_invalid_variable_name_is_not_installable_and_never_crashes(mk, bad_name):
    route = dict(LOCAL_ROUTE, auth={"type": "x", "env_vars": [{"name": bad_name, "role": "key-id", "secret": True}]})
    w = mk(findings=OLLAMA, catalog=[entry("alpaca", "Alpaca", [route])])
    receipt, io = w.run([*TOP, *AI_OLLAMA, "pick:alpaca", "none", "done", "do"])
    assert "setup details not curated yet" in "\n".join(io.said) and w.writes[0].plan.broker is None


def test_a_store_that_cannot_tell_gets_an_honest_message(mk):
    class Broken(FakeStore):
        def exists(self, name):
            raise OSError("nope")
    w = mk(findings=OLLAMA, store=Broken())
    io = ScriptedIO([*TOP, *AI_OLLAMA, "pick:alpaca", "continue"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert "I could not check" in io.prompts[-1][1] and "already in" not in io.prompts[-1][1]


# ------------------------------------------------------------------ B-3: Add and Skip really re-decide

KEYED = [*TOP, *AI_OLLAMA, "none", "lib:skill-keyed", "done"]


def test_skip_then_back_then_add_asks_for_the_key(mk):
    w = mk(findings=OLLAMA)
    receipt, io = w.run([*KEYED, "skip", "back", "add", "paste", K5, "do"])
    assert w.writes[0].values == {"FMP_API_KEY": K5}


def test_add_then_back_then_skip_does_not_write_the_key(mk):
    w = mk(findings=OLLAMA)
    receipt, io = w.run([*KEYED, "add", "paste", K5, "back", "skip", "do"])
    assert w.writes[0].values == {}
    assert "FMP_API_KEY" in "\n".join(io.said)
    assert_no_leak(io.everything(), K5)


# ------------------------------------------------------------------ B-2, B-6, R-d: honest copy

def test_broker_verify_does_not_promise_a_test_that_will_not_run(mk):
    w = mk(findings=OLLAMA)
    receipt, io = w.run([*TOP, *AI_OLLAMA, "pick:alpaca", "continue", "skip", "skip", "", "done", "do"])
    text = io.everything()
    assert "I will test the connection after setup" not in text and "not built yet" in text


def test_region_note_is_said(mk):
    cat = [{k: v for k, v in entry("alpaca", "Alpaca", [LOCAL_ROUTE]).items() if k != "region_codes"}]
    w = mk(findings=OLLAMA, catalog=cat)
    io = ScriptedIO([*TOP, *AI_OLLAMA, "search", "", "UK", "any"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert screens.REGION_NONE_CURATED in io.said


def test_cli_paragraph_appears_once_when_several_clis_are_found(mk):
    w = mk(findings=[cli_finding("claude"), cli_finding("codex")])
    io = ScriptedIO([*TOP])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert sum("I found" in s for s in io.said) == 1
    assert "Claude Code and Codex" in "\n".join(io.said)


def test_done_names_venice_and_the_receipt_path_and_review_flags_a_missing_key(mk):
    w = mk()
    receipt, io = w.run([*TOP, "paste-venice", "", "continue", "none", "done", "do"])
    review = next(p for k, p, _ in io.prompts if "exactly what I will do" in p)
    assert "AI: Venice (no key yet)" in review
    done = "\n".join(io.said)
    assert "AI: Venice." in done and "receipts" in done and "trader.json" in done


# ------------------------------------------------------------------ I9: nothing leaks at any interruption

class Boom(ScriptedIO):
    def __init__(self, answers, at, exc):
        super().__init__(answers)
        self.at, self.exc = at, exc

    def _next(self, kind, prompt, options=()):
        if len(self.prompts) == self.at:
            raise self.exc
        return super()._next(kind, prompt, options)


HAPPY_SECRETS = [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", True, False, "paste", K3, "true", "lib:skill-keyed",
                 "done", "add", "paste", K5, "do"]


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), EOFError(), ScriptExhausted("x")], ids=["ctrl-c", "eof", "exhausted"])
def test_interrupt_at_every_prompt_leaks_nothing(mk, exc):
    count = len(mk(environ=ALPACA_ENV, findings=[*OLLAMA, *ALPACA_FINDINGS]).run(HAPPY_SECRETS)[1].prompts)
    for at in range(count):
        w = mk(environ=ALPACA_ENV, findings=[*OLLAMA, *ALPACA_FINDINGS])
        w.state_path().unlink()
        io = Boom(list(HAPPY_SECRETS), at, exc)
        with pytest.raises(type(exc)) as info:
            run_wizard(io, w.deps)
        text = "".join(traceback.format_exception(info.value)) + io.everything() + w.state_text()
        assert_no_leak(text, K1, K2, K3, K5)
        sweep_tree(w.tmp, ALL_SECRETS)
        # and a restart from that point finishes without ever having kept a value
        io2 = ScriptedIO([])
        with pytest.raises((ScriptExhausted, KeyboardInterrupt, EOFError)):
            run_wizard(io2, w.deps)
        assert_no_leak(io2.everything(), K1, K2, K3, K5)


# ------------------------------------------------------------------ M-5, M-6, M-7, M-8

def test_state_file_holds_only_known_keys_with_non_secret_shapes_at_every_prompt(mk):
    w = mk(environ=ALPACA_ENV, findings=[*OLLAMA, *ALPACA_FINDINGS])
    answers = [*TOP, "paste-openrouter", K3, "continue", "pick:alpaca", "continue", True, False, "paste", K4, "true",
               "lib:skill-keyed", "done", "add", "paste", K5, "do"]
    receipt, io = w.run(answers, on_prompt=lambda io_: assert_state_is_clean(w.state_text()))
    assert receipt is not None
    assert_state_is_clean(w.state_text())


def test_a_rejected_paste_is_never_echoed_in_any_part(mk):
    rejected = K1[:10] + "${x}" + K1[10:]
    w = mk()
    receipt, io = w.run([*TOP, "paste-openrouter", rejected, K3, "continue", "none", "done", "do"])
    assert screens.BAD_VALUE in io.said
    assert_no_leak(io.everything() + w.state_text(), rejected)
    assert w.writes[0].values == {ORKEY: K3}


RAW_TOKENS = ("local-command", "remote-url", "client-prompt-only", "none-documented", "draft-then-execute")


def test_route_labels_and_orders_are_in_plain_words_never_raw_catalog_tokens(mk):
    raw = {"submit": {"can_submit_order": "yes", "confirmation": "client-prompt-only"},
           "read_only": {"available": True, "how": "a flag"}}
    two = entry("etoro", "Two", [dict(LOCAL_ROUTE, route_id="a", **raw),
                                 dict(LOCAL_ROUTE, route_id="b", submit={"can_submit_order": "yes",
                                                                         "confirmation": "none-documented"})])
    w = mk(findings=OLLAMA, catalog=[two])
    receipt, io = w.run([*TOP, *AI_OLLAMA, "pick:etoro", "route:a", "continue", "skip", "skip", "", "done", "do"])
    text = io.everything()
    for tok in RAW_TOKENS:
        assert tok not in text, tok
    assert "only a prompt in your AI app" in text and "runs a program on this computer" in text


def test_the_header_mapping_is_explained_once_in_plain_words(mk):
    import json as _j
    from pathlib import Path
    real = _j.loads((Path(__file__).parents[1] / "src" / "trading_agent" / "data" / "overlay.json").read_text())["tradier"]["routes"]
    w = mk(findings=OLLAMA, catalog=[entry("tradier", "Tradier", real)])
    receipt, io = w.run([*TOP, *AI_OLLAMA, "pick:tradier", "continue", "true", "skip", "done", "do"])
    said = "\n".join(io.said)
    assert "The Tradier page calls this PAPER_TRADING; in this profile it is stored as TRADIER_PAPER_TRADING." in said
    assert "The Tradier page calls this API_KEY; in this profile it is stored as TRADIER_API_KEY." in said


def test_not_installable_copy_does_not_over_promise_or_show_a_repo_path(mk):
    w = mk(findings=OLLAMA)
    io = ScriptedIO([*TOP, *AI_OLLAMA, "pick:kraken"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert "not installable here yet, I will point you to its page" in dict(io.prompts[-1][2])["pick:kraken"]
    assert "set you up" not in io.everything() and "I will show the steps" not in io.everything()
    nourl = entry("nourl", "No Url", [], selectable=False)
    nourl["source_url"] = ""
    w2 = mk(findings=OLLAMA, catalog=[nourl])
    w2.state_path().unlink()
    io2 = ScriptedIO([*TOP, *AI_OLLAMA, "all", "pick:nourl", "none"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io2, w2.deps)
    assert "see the broker's own page" in "\n".join(io2.said) and "brokers/nourl.md" not in "\n".join(io2.said)


def test_a_raw_version_line_never_reaches_the_screen_with_control_characters(mk):
    w = mk(hermes_kw={"version": "Hermes Agent v0.20.0\x1b[31m\x07" + "x" * 300})
    receipt, io = w.run(["start", "stop"])
    out = "\n".join(io.said)
    assert "\x1b" not in out and "\x07" not in out and "x" * 81 not in out and "hermes update" in out


def test_an_ambiguous_version_output_asks_instead_of_picking_one(mk):
    from trading_agent.profile.hermes_cli import UNREADABLE_VERSION
    w = mk(hermes_kw={"version": UNREADABLE_VERSION})
    io = ScriptedIO(["start"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert io.option_ids() == ["continue", "again", "stop", "back"]


# ------------------------------------------------------------------ N2: share questions terminate and never follow "Do it"

class AlwaysDifferentIO(ScriptedIO):
    """Answers "Enter a different value" at every share prompt, pasting a fresh key each time; the rest is scripted."""

    def __init__(self, answers):
        super().__init__(answers)
        self._pastes = 0
        self._paste_next = False

    def choose(self, prompt, options):
        if "will be able to read" in prompt:
            self.prompts.append(("choose", prompt, [(o.id, o.label) for o in options]))
            self._paste_next = True
            return "different"
        return super().choose(prompt, options)

    def secret(self, prompt):
        if self._paste_next:
            self._paste_next = False
            self.prompts.append(("secret", prompt, []))
            self._pastes += 1
            return (K2, K3)[self._pastes % 2]
        return super().secret(prompt)


def test_always_a_different_value_terminates_and_nothing_is_asked_after_do_it(mk):
    w = mk(environ={ORKEY: K1}, findings=[OR_FINDING], catalog=SHARED_CATALOG)
    io = AlwaysDifferentIO([*AI_THEN_BROKER, "done", "do", "do", "do"])
    run_wizard(io, w.deps)
    shares = [i for i, (k, p, _) in enumerate(io.prompts) if k == "choose" and "will be able to read" in p]
    assert 0 < len(shares) <= 12                                          # bounded, not 27
    reviews = [i for i, (k, p, _) in enumerate(io.prompts) if "Here is exactly what I will do" in p]
    last_review = reviews[-1]
    assert all(i < last_review for i in shares)                          # nothing asked after the final "Do it"
    for r, nxt in zip(reviews, reviews[1:]):                              # any question after a "Do it" got a fresh REVIEW
        assert nxt > r
    plan = w.writes[0].plan
    values = dict(w.writes[0].values)
    ai_on = plan.ai != AiChoice("none")
    assert ai_on != (plan.broker is not None)                            # exactly one consumer kept, the other refused
    assert ORKEY in values                                                # the kept consumer has its grant
    review = io.prompts[last_review][1]
    assert "left out" in review.lower()
    assert_no_leak(io.everything() + w.state_text(), K1, K2, K3)


# ------------------------------------------------------------------ N1: stale or forged left: records never reach DONE

def _seed(w, **decisions):
    """A hand-edited state.json: written raw, so only the loader's own checks apply."""
    path = w.state_path()
    doc = json.loads(path.read_text()) if path.exists() else {"current": "START", "decisions": {}}
    doc["decisions"].update(decisions)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def test_a_stale_left_out_record_does_not_survive_a_later_write(mk):
    w = mk(environ={ORKEY: K1}, findings=[OR_FINDING], catalog=SHARED_CATALOG)
    _seed(w, **{"left:ai": ORKEY})       # an earlier run left the AI out, then was undone before VERIFY
    receipt, io = w.run([*AI_THEN_BROKER, "same", "done", "do"])
    plan = w.writes[0].plan
    assert plan.ai == AiChoice("openrouter") and plan.broker is not None
    done = "\n".join(io.said)
    assert "left out" not in done.lower()
    assert not [k for k in json.loads(w.state_text())["decisions"] if k.startswith("left:")]


def test_a_forged_left_out_record_is_dropped_and_never_printed(mk):
    forged = "‮" + "A" * 511
    w = mk(environ={ORKEY: K1}, findings=[OR_FINDING], catalog=SHARED_CATALOG)
    _seed(w, **{"left:ai": forged, "left:evil": ORKEY, "left:broker:kraken/community": "lower case"})
    assert WizardState.load(w.platform).decisions["left:ai"] == forged   # the loader alone lets it through
    receipt, io = w.run([*AI_THEN_BROKER, "same", "done", "do"])
    text = io.everything()
    assert "\u202e" not in text and "A" * 64 not in text and "lower case" not in text and "a broker connection" not in text
    assert "left out" not in "\n".join(io.said).lower()
    assert not [k for k in json.loads(w.state_text())["decisions"] if k.startswith("left:")]
