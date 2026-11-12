from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakes import (
    ALL_SECRETS, K1, K2, K3, K4, K5, FakeHermes, FakePlatform, FakeStore, ScriptedIO, ScriptExhausted, assert_no_leak,
    cli_finding, env_finding, port_finding, sweep_tree,
)
from trading_agent.libraries import Library
from trading_agent.profile.hermes_cli import HermesNotBootstrapped, HermesUnavailable
from trading_agent.profile.receipt import Receipt
from trading_agent.profile.writer import AiChoice, ProfileExists, WriteError
from trading_agent.wizard import flow as flow_mod
from trading_agent.wizard import screens
from trading_agent.wizard.flow import Deps, Detectors, run_wizard
from trading_agent.wizard.state import State, WizardState

KEY1, KEY2, PASTED = K1, K2, K3
STATE_FILE = ("trading-agent", "state.json")

LOCAL_ROUTE = {
    "route_id": "local", "kind": "local-command", "maintainer": "official",
    "install": {"method": "uvx", "command_text": "uvx alpaca-mcp-server"},
    "auth": {"type": "api-key-plus-secret", "env_vars": [
        {"name": "ALPACA_API_KEY", "role": "key-id", "required": True, "secret": True},
        {"name": "ALPACA_SECRET_KEY", "role": "secret", "required": True, "secret": True},
        {"name": "ALPACA_PAPER_TRADE", "role": "mode-switch", "required": False, "secret": False},
    ]},
    "paper": {"supported": "yes", "default": "paper", "how_to_enable": "ALPACA_PAPER_TRADE (default true on the local server)"},
}
REMOTE_ROUTE = {
    "route_id": "hosted", "kind": "remote-url", "maintainer": "official", "url": "https://mcp.tradier.com/mcp",
    "auth": {"type": "bearer-token",
             "env_vars": [{"name": "TRADIER_PAPER_TRADING", "role": "mode-switch", "required": False, "secret": False}],
             "headers": ["API_KEY", "PAPER_TRADING"],
             "header_env": {"API_KEY": "TRADIER_API_KEY", "PAPER_TRADING": "TRADIER_PAPER_TRADING"},
             "key_scope_advice": "Use a paper token first."},
    "paper": {"supported": "yes", "default": "unconfirmed",
              "how_to_enable": "set the PAPER_TRADING header to true with a paper token"},
}
GEMINI_ROUTE = {
    "route_id": "api", "kind": "local-command", "maintainer": "community",
    "install": {"method": "uvx", "command_text": "uvx gemini-exchange-mcp"},
    "auth": {"type": "api-key-plus-secret", "env_vars": [
        {"name": "GEMINI_API_KEY", "role": "key-id", "required": True, "secret": True},
        {"name": "GEMINI_API_SECRET", "role": "secret", "required": True, "secret": True},
    ]},
}


def entry(slug, name, routes, *, selectable=True, status=("official",)):
    return {"slug": slug, "name": name, "directory": "brokers", "status": list(status), "trading": "true",
            "route_kinds": ["remote"], "region_raw": "US", "region_codes": ["US"],
            "source_url": f"https://example.test/{slug}", "page_path": f"brokers/{slug}.md",
            "last_verified": "2026-11-01", "routes": routes,
            "wizard": {"selectable": selectable, "banner": "setup details not curated yet"}}


CATALOG = [
    entry("zz-last", "Zz Last", [], selectable=False),
    entry("bitget", "Bitget", [], selectable=False),
    entry("alpaca", "Alpaca", [LOCAL_ROUTE]),
    entry("aaa-first", "Aaa First", [], selectable=False),
    entry("interactive-brokers", "Interactive Brokers", [], selectable=False),
    entry("kraken", "Kraken", [], selectable=False),
    entry("tradier", "Tradier", [REMOTE_ROUTE]),
    entry("gemini-exchange", "Gemini Exchange", [GEMINI_ROUTE]),
    entry("m1-finance", "M1 Finance", [], selectable=False, status=("none",)),
    entry("no-routes", "No Routes Yet", [], selectable=True),
]


def lib(lid, *, listed=True, needs=()):
    return Library(id=lid, kind="skills", name=f"Library {lid}", source_url="https://example.test/x", license="MIT",
                   counts={"n": 1}, needs_keys=tuple(needs), attribution="a", listed=listed)


LIBS = [lib("skill-a"), lib("skill-keyed", needs=("FMP_API_KEY",)), lib("secret-unlisted", listed=False)]


class World:
    def __init__(self, tmp_path, monkeypatch, *, environ=None, findings=(), catalog=None, libraries=None,
                 online=True, which=True, hermes_kw=None, store=None, write_error=None, verify_ai=None):
        self.tmp = tmp_path
        self.platform = FakePlatform(tmp_path / "home", environ=environ,
                                     which_map={"hermes": "/bin/hermes"} if which else {},
                                     config_home=tmp_path / "cfg")
        self.hermes = FakeHermes(self.platform, tmp_path / "hh", **(hermes_kw or {}))
        self.store = store or FakeStore()
        self.factory_dirs: list[Path] = []
        self.calls = {"env": 0, "mcp": [], "cli": 0, "ports": 0}
        fs = list(findings)
        kinds = {"env": ("ai-key", "broker-key", "data-key"), "cli": ("ai-cli",), "ports": ("ai-local", "gateway"),
                 "mcp": ("mcp-entry",)}

        def pick(which_):
            return [f for f in fs if f.kind in kinds[which_]]

        def det_env():
            self.calls["env"] += 1
            return pick("env")

        def det_mcp(enabled=True):
            self.calls["mcp"].append(enabled)
            return pick("mcp") if enabled else []

        def det_cli():
            self.calls["cli"] += 1
            return pick("cli")

        def det_ports():
            self.calls["ports"] += 1
            return pick("ports")

        kw = {}
        if verify_ai is not None:
            kw["verify_ai"] = verify_ai
        self.deps = Deps(
            platform=self.platform, hermes=self.hermes, detectors=Detectors(det_env, det_mcp, det_cli, det_ports),
            catalog=CATALOG if catalog is None else catalog, libraries=LIBS if libraries is None else libraries,
            store_factory=self.factory, source_dir=tmp_path / "dist", online=online, **kw)
        self.writes: list[SimpleNamespace] = []
        self.write_error = write_error
        monkeypatch.setattr(flow_mod, "write_profile", self._write)

    def factory(self, pdir):
        self.factory_dirs.append(Path(pdir))
        return self.store

    def _write(self, plan, *, hermes, source_dir, store_factory):
        self.writes.append(SimpleNamespace(plan=plan, values=dict(plan.env_values), hermes=hermes,
                                           source_dir=source_dir, store_factory=store_factory))
        if self.write_error is not None:
            err = self.write_error
            if callable(err):
                err = err(plan)
            raise err
        return Receipt(profile_name=plan.profile_name, steps_done=["install", "config", "broker", "secrets", "skills"],
                       env_names_written=sorted(plan.env_values),
                       ai_choice={"kind": plan.ai.kind, "ref": plan.ai.ref} if plan.ai else None,
                       broker={"slug": plan.broker.slug, "route_id": plan.broker.route_id} if plan.broker else None,
                       skills=list(plan.skills), run_id="r1")

    def state_path(self) -> Path:
        return self.tmp / "cfg" / Path(*STATE_FILE)

    def state_text(self) -> str:
        return self.state_path().read_text() if self.state_path().exists() else ""

    def run(self, answers, *, on_prompt=None):
        io = ScriptedIO(answers, on_prompt=on_prompt)
        return run_wizard(io, self.deps), io

    def trace(self):
        seen: list[State] = []

        def hook(io):
            seen.append(WizardState.load(self.platform).current)
        return seen, hook


@pytest.fixture
def mk(tmp_path, monkeypatch):
    def make(**kw):
        return World(tmp_path, monkeypatch, **kw)
    return make


OLLAMA = [port_finding("ollama")]
ALPACA_ENV = {"ALPACA_API_KEY": KEY1, "ALPACA_SECRET_KEY": KEY2}
ALPACA_FINDINGS = [env_finding("ALPACA_API_KEY", "broker-key"), env_finding("ALPACA_SECRET_KEY", "broker-key")]
TOP = ["start", "", "scan"]                       # START, PRECHECK (name), SCAN
AI_OLLAMA = ["use:ai-local:ollama", "continue"]   # AI_ROUTE, AI_VERIFY (the stub verifier: Continue only)
HAPPY = [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", True, True, "true",
         "lib:skill-a", "done", "do"]


def happy_world(mk, **kw):
    return mk(environ=ALPACA_ENV, findings=[*OLLAMA, *ALPACA_FINDINGS], **kw)


# ------------------------------------------------------------------ happy path and finish

def test_happy_path_one_local_model_one_broker(mk):
    w = happy_world(mk)
    receipt, io = w.run(HAPPY)
    assert isinstance(receipt, Receipt) and receipt.profile_name == "trader"
    assert len(w.writes) == 1
    plan = w.writes[0].plan
    assert plan.profile_name == "trader"
    assert plan.ai == AiChoice("ollama")
    assert plan.broker.slug == "alpaca" and plan.broker.route_id == "local" and plan.broker.entry == LOCAL_ROUTE
    assert plan.skills == ["skill-a"]
    assert w.writes[0].values == {"ALPACA_API_KEY": KEY1, "ALPACA_SECRET_KEY": KEY2, "ALPACA_PAPER_TRADE": "true"}
    assert w.writes[0].hermes is w.hermes and w.writes[0].source_dir == w.deps.source_dir
    assert WizardState.load(w.platform).current is State.DONE
    assert "You are set up." in io.everything()


def test_values_are_dropped_after_write(mk):
    w = happy_world(mk)
    w.run(HAPPY)
    assert dict(w.writes[0].plan.env_values) == {}  # the plan's own mapping was emptied


def test_nothing_found_finishes_with_a_banner(mk):
    w = mk()
    receipt, io = w.run([*TOP, "skip", "continue", "none", "done", "do"])
    assert receipt is not None
    plan = w.writes[0].plan
    assert plan.ai == AiChoice("none") and plan.broker is None and plan.skills == [] and dict(plan.env_values) == {}
    said = "\n".join(io.said)
    assert screens.FINISH_LATER_TITLE in said
    assert "AI" in said and "broker" in said.lower()
    assert WizardState.load(w.platform).current is State.DONE


def test_review_lists_exactly_what_will_happen_and_nothing_declined(mk):
    w = happy_world(mk)
    _, io = w.run(HAPPY)
    review = [p for k, p, _ in io.prompts if "Here is exactly what I will do" in p]
    assert review, "REVIEW prompt missing"
    text = review[0]
    assert "profile named trader" in text
    assert "Alpaca" in text
    assert "ALPACA_API_KEY" in text and "ALPACA_SECRET_KEY" in text and "ALPACA_PAPER_TRADE" in text
    assert "skill-a" in text
    assert "Change nothing in any other app or Hermes profile." in text
    assert_no_leak(text, KEY1, KEY2)


def test_review_text_for_declined_items(mk):
    w = mk(environ=ALPACA_ENV, findings=[*OLLAMA, *ALPACA_FINDINGS])
    answers = [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", False, "skip", True, "", "done"]
    io = ScriptedIO(answers)
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    review = [p for k, p, _ in io.prompts if "Here is exactly what I will do" in p][0]
    assert "ALPACA_SECRET_KEY" in review
    assert "ALPACA_API_KEY" not in review          # declined: absent
    assert "ALPACA_PAPER_TRADE" not in review      # skipped: absent


# ------------------------------------------------------------------ Back on every screen

PRE_AI = [*TOP, "use:ai-local:ollama"]
BACK_CASES = [
    ("PRECHECK", ["start", "<"], State.START),
    ("SCAN", ["start", "", "back"], State.PRECHECK),
    ("AI_ROUTE", [*TOP, "back"], State.SCAN),
    ("AI_VERIFY", [*PRE_AI, "back"], State.AI_ROUTE),
    ("BROKER_PICK", [*TOP, *AI_OLLAMA, "back"], State.AI_VERIFY),
    ("BROKER_INSTALL", [*TOP, *AI_OLLAMA, "pick:alpaca", "back"], State.BROKER_PICK),
    ("SKILLS-with-broker", [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", True, True, "true", "back"],
     State.BROKER_INSTALL),
    ("SKILLS-no-broker", [*TOP, *AI_OLLAMA, "none", "back"], State.BROKER_PICK),
    ("DATA_KEYS", [*TOP, *AI_OLLAMA, "none", "lib:skill-keyed", "done", "back"], State.SKILLS),
    ("REVIEW-no-keys", [*TOP, *AI_OLLAMA, "none", "lib:skill-a", "done", "back"], State.SKILLS),
    ("REVIEW-with-keys", [*TOP, *AI_OLLAMA, "none", "lib:skill-keyed", "done", "skip", "back"], State.DATA_KEYS),
]


@pytest.mark.parametrize("label,answers,expected", BACK_CASES, ids=[c[0] for c in BACK_CASES])
def test_every_screen_has_back(mk, label, answers, expected):
    w = happy_world(mk)
    seen, hook = w.trace()
    with pytest.raises(ScriptExhausted):
        w.run(answers, on_prompt=hook)
    assert seen[-1] is expected, f"after Back from {label} the next screen was {seen[-1]}"
    assert seen[-2].name == label.split("-")[0]


def test_back_options_exist_on_each_screen(mk):
    w = happy_world(mk)
    receipt, io = w.run(HAPPY)
    for k, prompt, opts in io.prompts:
        if k != "choose":
            continue
        ids = [i for i, _ in opts]
        if ids == ["start", "what"]:
            continue  # the first screen has nowhere to go back to
        assert "back" in ids, f"no Back on: {prompt[:60]!r} {ids}"


def test_back_from_start_is_not_offered_and_what_does_this_do_returns(mk):
    w = mk()
    seen, hook = w.trace()
    with pytest.raises(ScriptExhausted):
        w.run(["what"], on_prompt=hook)
    assert seen == [State.START, State.START]


# ------------------------------------------------------------------ resume

def test_resume_after_interrupt_at_broker_pick(mk):
    w = happy_world(mk)
    with pytest.raises(ScriptExhausted):
        w.run([*TOP, *AI_OLLAMA])  # interrupted when BROKER_PICK asks
    saved = WizardState.load(w.platform)
    assert saved.current is State.BROKER_PICK
    assert saved.decisions["ai.kind"] == "ollama"
    detectors_before = dict(w.calls, mcp=list(w.calls["mcp"]))
    receipt, io = w.run(["none", "done", "do"])
    assert receipt is not None
    assert w.calls["env"] == detectors_before["env"] and w.calls["ports"] == detectors_before["ports"]
    first = io.prompts[0]
    assert "broker" in first[1].lower()                       # resumed right at the broker screen
    assert not any("Set up your trading agent" in p for _, p, _ in io.prompts)
    assert w.writes[0].plan.ai == AiChoice("ollama")          # the earlier AI decision was kept, not re-asked
    assert w.writes[0].plan.profile_name == "trader"


def test_resume_reasks_pasted_values_and_never_persisted_them(mk):
    w = mk()
    answers = [*TOP, "paste-openrouter", PASTED, "continue", "none", "done"]
    with pytest.raises(ScriptExhausted):
        w.run(answers)                                        # interrupted at REVIEW
    assert WizardState.load(w.platform).current is State.REVIEW
    assert_no_leak(w.state_text(), PASTED)
    assert not any(k.startswith("ai:") for k in json.loads(w.state_text())["decisions"])   # a paste is not a decision
    receipt, io = w.run(["paste", PASTED, "do"])              # the wizard asks again, as it would for a new key
    assert io.kinds()[:2] == ["choose", "secret"] and "OPENROUTER_API_KEY" in io.prompts[0][1]
    assert w.writes[0].values == {"OPENROUTER_API_KEY": PASTED}


def test_resume_at_write_with_no_completed_receipt_goes_back_to_review(mk):
    w = mk()
    ws = WizardState(State.WRITE, {"profile_name": "trader", "ai.kind": "none", "broker.slug": "none",
                                   "skills.done": "1"})
    ws.save(w.platform)
    seen, hook = w.trace()
    with pytest.raises(ScriptExhausted):
        w.run([], on_prompt=hook)
    assert seen[-1] is State.REVIEW
    assert w.writes == []


def test_a_finished_run_starts_fresh_next_time(mk):
    w = happy_world(mk)
    w.run(HAPPY)
    assert WizardState.load(w.platform).current is State.DONE
    seen, hook = w.trace()
    with pytest.raises(ScriptExhausted):
        w.run([], on_prompt=hook)
    assert seen[-1] is State.START


# ------------------------------------------------------------------ reuse and consent

def test_reuse_requires_yes_and_names_destination(mk):
    w = happy_world(mk)
    receipt, io = w.run(HAPPY)
    confirms = [p for k, p, _ in io.prompts if k == "confirm"]
    assert len(confirms) == 2
    for prompt, name, value in zip(confirms, ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"), (KEY1, KEY2)):
        assert name in prompt and "profile" in prompt.lower() and ".env" in prompt
        assert_no_leak(prompt, KEY1, KEY2)
    assert_no_leak(io.everything() + w.state_text(), KEY1, KEY2)


def test_reuse_is_not_copied_when_the_user_says_no(mk):
    w = happy_world(mk)
    answers = [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", False, "skip", False, "skip", "",
               "done", "do"]
    receipt, io = w.run(answers)
    assert receipt is not None
    assert w.writes[0].values == {}
    assert_no_leak(io.everything(), KEY1, KEY2)


def test_not_this_one_is_remembered_per_finding_id(mk):
    w = mk(environ={"OPENROUTER_API_KEY": KEY1}, findings=[env_finding("OPENROUTER_API_KEY")])
    seen_ids = []
    answers = [*TOP, "use:env:OPENROUTER_API_KEY", False]
    io = ScriptedIO(answers + ["skip"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    decisions = json.loads(w.state_text())["decisions"]
    assert decisions["declined:env:OPENROUTER_API_KEY"] == "1"
    # the AI menu was shown again after the "no", and it no longer offers that finding
    menus = [ids for ids in (o for k, p, o in io.prompts if k == "choose" and "brain" in p.lower())]
    assert any("use:env:OPENROUTER_API_KEY" in [i for i, _ in m] for m in menus[:1])
    assert all("use:env:OPENROUTER_API_KEY" not in [i for i, _ in m] for m in menus[1:]) and len(menus) >= 2
    # and a later run (resume) does not bring it back or ask again
    io2 = ScriptedIO([])
    with pytest.raises(ScriptExhausted):
        run_wizard(io2, w.deps)
    assert "use:env:OPENROUTER_API_KEY" not in io2.option_ids(0)


def test_ai_key_reuse_when_yes_goes_to_the_new_profile_only(mk):
    w = mk(environ={"OPENROUTER_API_KEY": KEY1}, findings=[env_finding("OPENROUTER_API_KEY")])
    answers = [*TOP, "use:env:OPENROUTER_API_KEY", True, "continue", "none", "done", "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].plan.ai == AiChoice("openrouter")
    assert w.writes[0].values == {"OPENROUTER_API_KEY": KEY1}
    confirm = [p for k, p, _ in io.prompts if k == "confirm"][0]
    assert "OPENROUTER_API_KEY" in confirm and "profile" in confirm.lower()
    assert_no_leak(confirm, KEY1)
    assert_no_leak(io.everything() + w.state_text(), KEY1)


def test_login_shell_only_finding_cannot_be_copied_so_paste_is_offered(mk):
    f = env_finding("ALPACA_API_KEY", "broker-key", where="login shell environment")
    w = mk(environ={}, findings=[*OLLAMA, f])
    answers = [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", "paste", PASTED, "skip", "",
               "done", "do"]
    receipt, io = w.run(answers)
    assert "confirm" not in io.kinds()                         # no "copy it" question: nothing to copy from
    first_key_prompt = next(o for k, p, o in io.prompts if k == "choose" and "ALPACA_API_KEY" in p)
    assert [i for i, _ in first_key_prompt] == ["paste", "skip"]
    assert "login shell" in io.everything().lower() and "paste" in io.everything().lower()
    assert w.writes[0].values == {"ALPACA_API_KEY": PASTED}
    assert_no_leak(io.everything() + w.state_text(), PASTED)


def test_environ_value_vanishing_after_consent_falls_back_to_paste(mk):
    w = mk(environ={"OPENROUTER_API_KEY": ""}, findings=[env_finding("OPENROUTER_API_KEY")])
    answers = [*TOP, "use:env:OPENROUTER_API_KEY", True, PASTED, "continue", "none", "done", "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].values == {"OPENROUTER_API_KEY": PASTED}


def test_reference_only_finding_is_never_offered_for_copy(mk):
    route = dict(LOCAL_ROUTE, auth={"type": "x", "env_vars": [
        {"name": "CAP_API_PASSWORD", "role": "secret", "required": True, "secret": True}]})
    cat = [entry("capital-com", "Capital", [route])]
    f = env_finding("CAP_API_PASSWORD", "broker-key", reusable="reference-only")
    w = mk(environ={"CAP_API_PASSWORD": KEY1}, findings=[*OLLAMA, f], catalog=cat)
    answers = [*TOP, *AI_OLLAMA, "pick:capital-com", "continue", "paste", PASTED, "done", "do"]
    receipt, io = w.run(answers)
    assert "confirm" not in io.kinds()
    assert "I will not copy this one" in io.everything()
    assert w.writes[0].values == {"CAP_API_PASSWORD": PASTED}


# ------------------------------------------------------------------ Gemini scoping

def test_gemini_exchange_key_is_never_offered_as_an_ai_key(mk):
    gem = [env_finding("GEMINI_API_KEY", "broker-key"), env_finding("GEMINI_API_SECRET", "broker-key")]
    w = mk(environ={"GEMINI_API_KEY": KEY1, "GEMINI_API_SECRET": KEY2}, findings=gem)
    receipt, io = w.run([*TOP, "skip", "continue", "none", "done", "do"])
    ai_menu = next(o for k, p, o in io.prompts if k == "choose" and "brain" in p.lower())
    assert not any("GEMINI" in i or "GEMINI" in label for i, label in ai_menu)
    assert not any("confirm" == k for k in io.kinds())
    assert "GEMINI" not in "\n".join(p for k, p, _ in io.prompts if k == "choose" and "brain" in p.lower())


def test_gemini_exchange_key_is_offered_in_the_broker_step_only(mk):
    gem = [env_finding("GEMINI_API_KEY", "broker-key"), env_finding("GEMINI_API_SECRET", "broker-key")]
    w = mk(environ={"GEMINI_API_KEY": KEY1, "GEMINI_API_SECRET": KEY2}, findings=gem)
    answers = [*TOP, "skip", "continue", "all", "pick:gemini-exchange", "continue", True, True, "done", "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].values == {"GEMINI_API_KEY": KEY1, "GEMINI_API_SECRET": KEY2}
    assert [p for k, p, _ in io.prompts if k == "confirm"][0].count("GEMINI_API_KEY") == 1


def test_an_ai_key_name_is_not_offered_for_a_broker_route(mk):
    route = dict(LOCAL_ROUTE, auth={"type": "x", "env_vars": [
        {"name": "OPENAI_API_KEY", "role": "secret", "required": True, "secret": True}]})
    w = mk(environ={"OPENAI_API_KEY": KEY1}, findings=[env_finding("OPENAI_API_KEY", "ai-key")],
           catalog=[entry("kraken", "Odd", [route])])
    answers = [*TOP, "skip", "continue", "pick:kraken", "continue", "skip", "done", "do"]
    receipt, io = w.run(answers)
    assert "confirm" not in io.kinds()


# ------------------------------------------------------------------ AI step (R23, R26)

def test_cli_found_is_explained_and_cannot_be_chosen_as_a_provider(mk):
    w = mk(findings=[cli_finding("claude")])
    receipt, io = w.run([*TOP, "skip", "continue", "none", "done", "do"])
    said = "\n".join(io.said + io.prompt_texts())
    assert ("I found Claude Code on this computer. Right now it cannot power this agent by itself. "
            "Pick a local model or an API key for the agent's brain; your Claude sign-in stays untouched.") in said
    ai_menu = next(o for k, p, o in io.prompts if k == "choose" and "brain" in p.lower())
    assert not any(i.startswith("cli") or i.startswith("use:cli") for i, _ in ai_menu)
    # an explicit "Skip for now" while a CLI is around is the only way a cli choice is recorded
    assert w.writes[0].plan.ai == AiChoice("cli", "claude")


def test_skip_without_a_cli_records_none(mk):
    w = mk()
    w.run([*TOP, "skip", "continue", "none", "done", "do"])
    assert w.writes[0].plan.ai == AiChoice("none")


def test_hero_is_a_running_local_model_then_a_key_then_a_paste_card(mk):
    w = mk(environ={"OPENROUTER_API_KEY": KEY1}, findings=[port_finding("lm-studio"), port_finding("ollama"),
                                                          env_finding("OPENROUTER_API_KEY")])
    io = ScriptedIO([*TOP])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    ids = io.option_ids()
    assert ids[0] == "use:ai-local:ollama" and ids[1] == "use:ai-local:lm-studio"
    assert ids.index("use:env:OPENROUTER_API_KEY") < ids.index("paste-openrouter")
    assert "Found on this computer" in "\n".join(label for _, label in io.prompts[-1][2])

    w2 = mk()
    w2.state_path().unlink()           # same temp dir as the first world: forget its saved state
    io2 = ScriptedIO([*TOP])
    with pytest.raises(ScriptExhausted):
        run_wizard(io2, w2.deps)
    assert io2.option_ids()[0] == "paste-openrouter"          # no key and no model: the paste card leads
    assert "https://openrouter.ai/keys" in io2.everything()


def test_nothing_is_precheck_marked(mk):
    w = happy_world(mk)
    io = ScriptedIO([*TOP])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    text = io.everything()
    assert "[x]" not in text and "[X]" not in text and "(selected)" not in text


def test_ai_option_ids_are_unique_and_local_models_write_no_key(mk):
    w = mk(findings=[port_finding("lm-studio")])
    receipt, io = w.run([*TOP, "use:ai-local:lm-studio", "continue", "none", "done", "do"])
    assert w.writes[0].plan.ai == AiChoice("lm-studio") and dict(w.writes[0].values) == {}


def test_private_toggle_shows_only_local_models_and_venice(mk):
    w = mk(environ={"OPENROUTER_API_KEY": KEY1, "VENICE_API_KEY": KEY2},
           findings=[port_finding("ollama"), env_finding("OPENROUTER_API_KEY"), env_finding("VENICE_API_KEY")])
    io = ScriptedIO([*TOP, "private"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    ids = io.option_ids()
    assert "use:ai-local:ollama" in ids and "use:env:VENICE_API_KEY" in ids
    assert not any("OPENROUTER" in i or i in ("paste-openrouter", "custom") for i in ids)


def test_venice_key_becomes_a_custom_endpoint_with_its_key_name(mk):
    w = mk(environ={"VENICE_API_KEY": KEY1}, findings=[env_finding("VENICE_API_KEY")])
    receipt, io = w.run([*TOP, "use:env:VENICE_API_KEY", True, "continue", "none", "done", "do"])
    assert w.writes[0].plan.ai == AiChoice("custom", "https://api.venice.ai/api/v1", "VENICE_API_KEY")
    assert w.writes[0].values == {"VENICE_API_KEY": KEY1}


def test_custom_endpoint_asks_address_and_key_name_only(mk):
    w = mk()
    answers = [*TOP, "custom", "http://localhost:9000/v1", "MY_LLM_KEY", PASTED, "continue", "none", "done", "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].plan.ai == AiChoice("custom", "http://localhost:9000/v1", "MY_LLM_KEY")
    assert w.writes[0].values == {"MY_LLM_KEY": PASTED}
    assert_no_leak(io.everything(), PASTED)


def test_custom_key_name_that_looks_like_a_pasted_key_is_refused_without_echo(mk):
    w = mk()
    pasted_key = "ABCDEF0123456789ABCDEF0123456789"     # all capitals and digits would pass a plain NAME check
    answers = [*TOP, "custom", "http://localhost:9000/v1", pasted_key, "MY_LLM_KEY", PASTED, "continue", "none", "done", "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].plan.ai.key_env == "MY_LLM_KEY"
    assert_no_leak(io.everything() + w.state_text(), pasted_key)


def test_only_a_catalog_marked_mode_switch_is_typed_in_plain_and_only_from_the_allowlist(mk):
    route = dict(REMOTE_ROUTE, auth={"type": "x", "headers": ["SANDBOX_TOKEN", "PAPER_TRADING"], "env_vars": [
        {"name": "DEMO_MODE", "role": "mode-switch", "required": False, "secret": False}]})
    w = mk(findings=OLLAMA, catalog=[entry("etoro", "E", [route])])
    answers = [*TOP, *AI_OLLAMA, "pick:etoro", "continue", "demo", "paper", "paste", PASTED, "paste", KEY2,
               "done", "do"]
    receipt, io = w.run(answers)
    by_name = {n: [k for k, p, _ in io.prompts if n in p and k in ("ask", "secret")]
               for n in ("DEMO_MODE", "SANDBOX_TOKEN", "PAPER_TRADING")}
    assert by_name == {"DEMO_MODE": ["ask", "ask"], "SANDBOX_TOKEN": ["secret"], "PAPER_TRADING": ["secret"]}
    assert screens.MODE_NOT_OK in io.said                      # "demo" is not on the allowlist
    assert w.writes[0].values == {"DEMO_MODE": "paper", "SANDBOX_TOKEN": PASTED, "PAPER_TRADING": KEY2}
    assert_no_leak(io.everything(), PASTED, KEY2)


def test_custom_endpoint_rejects_a_bad_address_without_echoing_it(mk):
    w = mk()
    bad = "ftp://user:hunter2@host/x"
    answers = [*TOP, "custom", bad, "http://localhost:9000/v1", "", "continue", "none", "done", "do"]
    receipt, io = w.run(answers)
    assert "hunter2" not in io.everything() and bad not in io.everything()
    assert w.writes[0].plan.ai == AiChoice("custom", "http://localhost:9000/v1", "")


def test_openrouter_paste_card_has_instructions_and_never_echoes(mk):
    w = mk()
    receipt, io = w.run([*TOP, "paste-openrouter", PASTED, "continue", "none", "done", "do"])
    assert w.writes[0].plan.ai == AiChoice("openrouter") and w.writes[0].values == {"OPENROUTER_API_KEY": PASTED}
    assert "https://openrouter.ai/keys" in io.everything()
    assert_no_leak(io.everything() + w.state_text(), PASTED)


def test_pasted_value_that_the_env_file_would_refuse_is_asked_again_without_echo(mk):
    w = mk()
    bad = "has ${expansion} inside"
    receipt, io = w.run([*TOP, "paste-openrouter", bad, PASTED, "continue", "none", "done", "do"])
    assert bad not in io.everything() and "expansion" not in io.everything()
    assert w.writes[0].values == {"OPENROUTER_API_KEY": PASTED}


def test_empty_paste_skips_that_key_and_lists_it_for_later(mk):
    w = mk()
    receipt, io = w.run([*TOP, "paste-openrouter", "", "continue", "none", "done", "do"])
    assert w.writes[0].values == {}
    assert "OPENROUTER_API_KEY" in "\n".join(io.said)


def test_anthropic_key_is_information_only(mk):
    w = mk(environ={"ANTHROPIC_API_KEY": KEY1}, findings=[env_finding("ANTHROPIC_API_KEY")])
    io = ScriptedIO([*TOP])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert not any("ANTHROPIC" in i for i in io.option_ids())
    assert "ANTHROPIC_API_KEY" in "\n".join(io.said)


def test_ai_verify_stub_does_not_promise_a_test_and_injected_verifier_does(mk):
    w = mk(findings=OLLAMA)
    io = ScriptedIO([*TOP, "use:ai-local:ollama"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert io.option_ids() == ["continue", "back"]            # no "Test it" while nothing can test
    assert "not built yet" in io.prompts[-1][1] and "Check that it answers" not in io.everything()
    calls = []

    def verify(choice):
        calls.append(choice)
        return "passed"
    w2 = mk(findings=OLLAMA, verify_ai=verify)
    w2.state_path().unlink()
    receipt, io2 = w2.run([*TOP, "use:ai-local:ollama", "test", "none", "done", "do"])
    assert calls == [AiChoice("ollama")] and "Check that it answers" in io2.everything()


def test_nothing_to_test_prompt_does_not_ask_to_check(mk):
    w = mk()
    io = ScriptedIO([*TOP, "skip"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert "Nothing to test" in io.prompts[-1][1] and "Check that it answers" not in io.everything()


# ------------------------------------------------------------------ offline

def test_offline_marks_finish_later(mk):
    w = happy_world(mk, online=False)
    answers = [*TOP, "use:ai-local:ollama", "continue", "pick:alpaca", "continue", True, True, "true",
               "lib:skill-a", "done", "do"]
    receipt, io = w.run(answers)
    said = "\n".join(io.said)
    assert receipt is not None and WizardState.load(w.platform).current is State.DONE
    assert said.count("Needs internet") >= 2
    later = said[said.index(screens.FINISH_LATER_TITLE):]
    assert "Needs internet" in later


def test_offline_ai_verify_offers_no_test(mk):
    w = mk(findings=OLLAMA, online=False)
    io = ScriptedIO([*TOP, "use:ai-local:ollama"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert io.option_ids() == ["continue", "back"]
    assert "Needs internet" in "\n".join(io.said)


# ------------------------------------------------------------------ PRECHECK

def test_precheck_looks_for_the_binary_first_and_never_runs_a_missing_one(mk):
    w = mk(which=False)
    receipt, io = w.run(["start", "stop"])
    assert receipt is None
    assert w.hermes.version_calls == 0
    assert w.platform.calls.count("which") >= 1 and w.hermes.version_calls == 0   # looked, never ran
    said = "\n".join(io.said)
    assert screens.INSTALL_CMD == "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
    assert screens.INSTALL_CMD in said and "not installed yet" in said
    assert io.option_ids() == ["again", "stop", "back"]
    assert WizardState.load(w.platform).current is State.PRECHECK


def test_precheck_check_again_picks_up_a_fresh_install(mk):
    w = mk(which=False)

    def installed(io):
        if len(io.prompts) == 2:  # the user installs while the "Check again" prompt is open
            w.platform._which["hermes"] = "/bin/hermes"
    receipt, io = w.run(["start", "again", "", "scan", "skip", "continue", "none", "done", "do"],
                        on_prompt=installed)
    assert receipt is not None and w.hermes.version_calls >= 1


def test_precheck_never_executes_the_install_command(mk):
    w = mk(which=False)
    w.run(["start", "stop"])
    assert w.platform.run_calls == []


def test_precheck_not_bootstrapped_continues(mk):
    w = mk(hermes_kw={"raises": HermesNotBootstrapped("x")})
    receipt, io = w.run(["start", "", "scan", "skip", "continue", "none", "done", "do"])
    assert receipt is not None
    assert "Hermes found, data root not initialized yet" in "\n".join(io.said)


def test_precheck_plain_hermes_failure_waits_instead_of_continuing(mk):
    w = mk(hermes_kw={"raises": HermesUnavailable("x")})
    receipt, io = w.run(["start", "stop"])
    assert receipt is None and io.option_ids() == ["again", "stop", "back"]
    assert "did not answer" in "\n".join(io.said)


def test_precheck_version_below_the_pin_shows_the_update_command_as_text(mk):
    w = mk(hermes_kw={"version": "Hermes Agent v0.22.9 (2026.8.1)"})
    receipt, io = w.run(["start", "stop"])
    said = "\n".join(io.said)
    assert receipt is None
    assert ">=0.23.0" in said and "v0.22.9" in said and "hermes update" in said
    assert w.platform.run_calls == []                          # text only, never run


def test_precheck_exactly_the_pin_passes(mk):
    w = mk(hermes_kw={"version": "Hermes Agent v0.23.0"})
    io = ScriptedIO(["start"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert io.kinds()[-1] == "ask" and "hermes update" not in "\n".join(io.said)


def test_precheck_unreadable_version_continues_with_a_note(mk):
    w = mk(hermes_kw={"version": "Hermes Agent (dev build)"})
    receipt, io = w.run(["start", "continue", "", "scan", "skip", "continue", "none", "done", "do"])
    assert receipt is not None and "could not read the hermes version" in "\n".join(io.said).lower()
    assert io.option_ids(1) == ["continue", "again", "stop", "back"]      # it asks; it does not wave it through


@pytest.mark.parametrize("bad", ["Bad Name!", "1abc", "x" * 40, "../etc", "tr\nader"])
def test_profile_name_reprompts_without_echoing_odd_content(mk, bad):
    w = mk()
    receipt, io = w.run(["start", bad, "", "scan", "skip", "continue", "none", "done", "do"])
    assert receipt is not None and w.writes[0].plan.profile_name == "trader"
    assert bad not in io.everything()


def test_reserved_profile_name_reprompts(mk):
    w = mk()
    receipt, io = w.run(["start", "root", "", "scan", "skip", "continue", "none", "done", "do"])
    assert w.writes[0].plan.profile_name == "trader"
    assert io.kinds().count("ask") == 2


def test_default_profile_name_is_trader_and_a_custom_one_is_used(mk):
    w = mk()
    w.run(["start", "my-agent", "scan", "skip", "continue", "none", "done", "do"])
    assert w.writes[0].plan.profile_name == "my-agent"
    assert screens.DEFAULT_PROFILE_NAME == "trader"


def test_name_taken_offers_rename_or_back_only(mk):
    w = mk(hermes_kw={"existing": ["trader"]})
    receipt, io = w.run(["start", "", "rename", "second", "scan", "skip", "continue", "none", "done", "do"])
    assert [i for i, _ in next(o for k, p, o in io.prompts if k == "choose" and "already have a profile" in p)] == \
        ["rename", "back"]
    assert "I will not overwrite it" in "\n".join(io.said + io.prompt_texts())
    assert w.writes[0].plan.profile_name == "second"


# ------------------------------------------------------------------ SCAN

def test_skip_scanning_other_apps_turns_off_the_mcp_parse(mk):
    w = mk(findings=OLLAMA)
    with pytest.raises(ScriptExhausted):
        w.run([*TOP[:2], "scan-skip-apps", "skip"])
    assert w.calls["mcp"] == [False]


def test_scan_parses_other_apps_only_after_the_user_chooses_to(mk):
    w = mk()
    with pytest.raises(ScriptExhausted):
        w.run(["start", "", "scan", "skip"])
    assert w.calls["mcp"] == [True]


def test_a_failing_detector_shows_could_not_check_and_setup_continues(mk):
    w = mk()

    def boom():
        raise RuntimeError("disk on fire " + KEY1)
    w.deps.detectors = Detectors(boom, w.deps.detectors.mcp_files, w.deps.detectors.ai_clis, w.deps.detectors.ports)
    receipt, io = w.run([*TOP, "skip", "continue", "none", "done", "do"])
    assert receipt is not None
    assert "Could not check" in "\n".join(io.said)
    assert "disk on fire" not in io.everything()
    assert_no_leak(io.everything(), KEY1)


def test_scan_prints_names_only(mk):
    w = mk(environ=ALPACA_ENV, findings=[*OLLAMA, *ALPACA_FINDINGS])
    io = ScriptedIO([*TOP])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    said = "\n".join(io.said)
    assert "ALPACA_API_KEY" in said
    assert_no_leak(said, KEY1, KEY2)
    assert "Nothing here has been used yet" in said


# ------------------------------------------------------------------ brokers

def test_first_screen_lists_no_broker_then_the_curated_ten_that_exist_in_curated_order(mk):
    w = mk(findings=OLLAMA)
    io = ScriptedIO([*TOP, *AI_OLLAMA])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert io.option_ids() == ["none", "pick:interactive-brokers", "pick:alpaca", "pick:kraken", "pick:tradier",
                               "pick:bitget", "all", "search", "back"]   # curated order, not A to Z


def test_non_installable_first_screen_entries_say_so_in_plain_words(mk):
    w = mk(findings=OLLAMA)
    io = ScriptedIO([*TOP, *AI_OLLAMA])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    labels = dict(io.prompts[-1][2])
    assert screens.TAG_SELF_SETUP in labels["pick:kraken"] and screens.TAG_SELF_SETUP not in labels["pick:alpaca"]


def test_all_lists_every_broker_curated_first_then_a_to_z(mk):
    w = mk(findings=OLLAMA)
    io = ScriptedIO([*TOP, *AI_OLLAMA, "all", "pick:m1-finance"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert io.option_ids(-2) == [
        "pick:interactive-brokers", "pick:alpaca", "pick:kraken", "pick:tradier", "pick:bitget",
        "pick:aaa-first", "pick:gemini-exchange", "pick:m1-finance", "pick:no-routes", "pick:zz-last", "back"]


def test_non_selectable_broker_cannot_be_installed_and_shows_the_banner(mk):
    w = mk(findings=OLLAMA)
    seen, hook = w.trace()
    answers = [*TOP, *AI_OLLAMA, "all", "pick:m1-finance", "none", "done", "do"]
    receipt, io = w.run(answers, on_prompt=hook)
    said = "\n".join(io.said)
    assert "setup details not curated yet" in said
    assert "https://example.test/m1-finance" in said and "brokers/m1-finance.md" not in said
    assert "set this up yourself" in said
    assert w.writes[0].plan.broker is None
    assert State.BROKER_INSTALL not in seen


def test_selectable_entry_with_no_routes_is_not_installable_either(mk):
    w = mk(findings=OLLAMA)
    seen, hook = w.trace()
    receipt, io = w.run([*TOP, *AI_OLLAMA, "all", "pick:no-routes", "none", "done", "do"], on_prompt=hook)
    assert w.writes[0].plan.broker is None and State.BROKER_INSTALL not in seen
    assert "setup details not curated yet" in "\n".join(io.said)


def test_search_flow_filters_and_keeps_uncurated_visible(mk):
    cat = [dict(entry("uk-one", "UK One", [REMOTE_ROUTE]), region_codes=["UK"]),
           dict(entry("us-one", "US One", [REMOTE_ROUTE]), region_codes=["US"]),
           {k: v for k, v in entry("odd-one", "Odd One", [REMOTE_ROUTE]).items() if k != "region_codes"}]
    w = mk(findings=OLLAMA, catalog=cat)
    io = ScriptedIO([*TOP, *AI_OLLAMA, "search", "", "UK", "any"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    ids = io.option_ids()
    assert ids == ["pick:uk-one", "pick:odd-one", "back"]
    assert "region not curated" in "\n".join(label for _, label in io.prompts[-1][2]).lower()


def test_multiple_routes_ask_which_one(mk):
    two = entry("etoro", "Two", [dict(REMOTE_ROUTE, route_id="hosted-live"), dict(REMOTE_ROUTE, route_id="hosted-paper")])
    w = mk(findings=OLLAMA, catalog=[two])
    answers = [*TOP, *AI_OLLAMA, "pick:etoro", "route:hosted-paper", "continue", "", "skip", "done", "do"]
    w.run(answers)
    assert w.writes[0].plan.broker.route_id == "hosted-paper"


def test_every_env_var_and_header_name_is_asked_including_mode_switches(mk):
    w = mk(findings=OLLAMA)
    answers = [*TOP, *AI_OLLAMA, "pick:tradier", "continue", "true", "paste", PASTED, "done", "do"]
    receipt, io = w.run(answers)
    asked = "\n".join(p for k, p, _ in io.prompts if k in ("secret", "ask", "choose", "confirm"))
    assert "TRADIER_API_KEY" in asked and "TRADIER_PAPER_TRADING" in asked
    assert "set the PAPER_TRADING header to true with a paper token" in "\n".join(io.said)   # the catalog's own wording
    assert w.writes[0].values == {"TRADIER_API_KEY": PASTED, "TRADIER_PAPER_TRADING": "true"}
    assert "will not start until it is set" in "\n".join(io.said)
    assert_no_leak(io.everything(), PASTED)
    assert next(k for k, p, _ in io.prompts if "TRADIER_PAPER_TRADING" in p and k in ("ask", "secret")) == "ask"
    assert any(p.startswith("Tradier: ") for k, p, _ in io.prompts)     # every value prompt names the broker
    assert next(k for k, p, _ in io.prompts if "TRADIER_API_KEY" in p and k in ("ask", "secret")) == "secret"


def test_local_route_mode_switch_is_typed_in_plain_and_secrets_hidden(mk):
    w = mk(findings=OLLAMA)
    answers = [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", "paste", PASTED, "paste", KEY2, "false",
               "done", "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].values == {"ALPACA_API_KEY": PASTED, "ALPACA_SECRET_KEY": KEY2, "ALPACA_PAPER_TRADE": "false"}
    assert [k for k, p, _ in io.prompts if "ALPACA_PAPER_TRADE" in p and k in ("ask", "secret")] == ["ask"]
    assert "ALPACA_PAPER_TRADE (default true on the local server)" in "\n".join(io.said)


def test_skipped_names_are_listed_under_finish_later_and_explained(mk):
    w = mk(findings=OLLAMA)
    answers = [*TOP, *AI_OLLAMA, "pick:tradier", "continue", "", "skip", "done", "do"]
    receipt, io = w.run(answers)
    later = "\n".join(io.said)
    later = later[later.index(screens.FINISH_LATER_TITLE):]
    assert "TRADIER_API_KEY" in later and "TRADIER_PAPER_TRADING" in later and "Tradier" in later
    assert w.writes[0].values == {}


def test_existing_name_in_the_profile_env_asks_keep_or_replace(mk):
    store = FakeStore(existing={"ALPACA_API_KEY"})
    w = happy_world(mk, store=store)
    answers = [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", "keep", True, "true", "lib:skill-a", "done", "do"]
    receipt, io = w.run(answers)
    assert w.factory_dirs and all(d == w.hermes.profile_dir("trader") for d in w.factory_dirs)
    assert "ALPACA_API_KEY" not in w.writes[0].values            # kept: not overwritten
    assert w.writes[0].values["ALPACA_SECRET_KEY"] == KEY2
    prompt = next(o for k, p, o in io.prompts if k == "choose" and "already" in p and "ALPACA_API_KEY" in p)
    assert [i for i, _ in prompt] == ["keep", "replace"]
    review = [p for _, p, _ in io.prompts if "Here is exactly what I will do" in p][0]
    assert "ALPACA_API_KEY" not in review


def test_existing_name_replace_goes_on_to_get_a_value(mk):
    store = FakeStore(existing={"ALPACA_API_KEY"})
    w = happy_world(mk, store=store)
    answers = [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", "replace", True, True, "true", "lib:skill-a",
               "done", "do"]
    w.run(answers)
    assert w.writes[0].values["ALPACA_API_KEY"] == KEY1


def test_collision_check_happens_before_any_secret_is_asked(mk):
    store = FakeStore(existing={"OPENROUTER_API_KEY"})
    w = mk(environ={"OPENROUTER_API_KEY": KEY1}, findings=[env_finding("OPENROUTER_API_KEY")], store=store)
    io = ScriptedIO([*TOP, "use:env:OPENROUTER_API_KEY"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    assert io.kinds()[-1] == "choose" and "already" in io.prompts[-1][1]


def test_changing_the_broker_forgets_the_old_brokers_value_decisions(mk):
    w = mk(findings=OLLAMA)
    # first leave Alpaca half configured, then go back and choose "none": its names must not reach the plan
    answers = [*TOP, *AI_OLLAMA, "pick:alpaca", "continue", "paste", PASTED, "skip", "", "back", "back", "none",
               "done", "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].plan.broker is None and w.writes[0].values == {}


# ------------------------------------------------------------------ skills and data keys (R27)

def test_unlisted_library_is_never_offered(mk):
    w = mk(findings=OLLAMA)
    io = ScriptedIO([*TOP, *AI_OLLAMA, "none"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    ids = io.option_ids()
    assert "lib:skill-a" in ids and "lib:skill-keyed" in ids and "lib:secret-unlisted" not in ids
    assert "secret-unlisted" not in io.everything() and "Library secret-unlisted" not in io.everything()


def test_picking_libraries_toggles_and_nothing_starts_picked(mk):
    w = mk(findings=OLLAMA)
    io = ScriptedIO([*TOP, *AI_OLLAMA, "none", "lib:skill-a", "lib:skill-a", "lib:skill-keyed", "done"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    first = "\n".join(label for _, label in io.prompts[-5][2])
    assert "[x]" not in first
    last = "\n".join(label for _, label in io.prompts[-2][2])
    assert last.count("[x]") == 1                              # picked, unpicked, then another picked
    assert io.option_ids() == ["add", "skip", "back"]


def test_keys_are_asked_only_for_the_picked_libraries(mk):
    w = mk(findings=OLLAMA)
    answers = [*TOP, *AI_OLLAMA, "none", "lib:skill-keyed", "done", "add", "paste", PASTED, "do"]
    receipt, io = w.run(answers)
    assert w.writes[0].values == {"FMP_API_KEY": PASTED}
    assert w.writes[0].plan.skills == ["skill-keyed"]
    asked = [p for k, p, _ in io.prompts if k in ("secret", "choose") and "FMP_API_KEY" in p]
    assert asked and not any("FINVIZ" in p or "TD_API_KEY" in p for p in asked)


def test_no_picks_means_no_key_questions(mk):
    w = mk(findings=OLLAMA)
    receipt, io = w.run([*TOP, *AI_OLLAMA, "none", "done", "do"])
    assert "secret" not in io.kinds() and "confirm" not in io.kinds()
    assert w.writes[0].values == {}


def test_data_key_can_be_skipped(mk):
    w = mk(findings=OLLAMA)
    receipt, io = w.run([*TOP, *AI_OLLAMA, "none", "lib:skill-keyed", "done", "skip", "do"])
    assert w.writes[0].values == {}
    later = "\n".join(io.said)
    assert "FMP_API_KEY" in later[later.index(screens.FINISH_LATER_TITLE):]


def test_data_key_reuse_follows_the_same_consent_card(mk):
    w = mk(environ={"FMP_API_KEY": KEY1}, findings=[*OLLAMA, env_finding("FMP_API_KEY", "data-key")])
    receipt, io = w.run([*TOP, *AI_OLLAMA, "none", "lib:skill-keyed", "done", "add", True, "do"])
    assert w.writes[0].values == {"FMP_API_KEY": KEY1}
    assert_no_leak(io.everything(), KEY1)


# ------------------------------------------------------------------ WRITE outcomes

def test_profile_exists_at_write_offers_rename_or_back_only(mk):
    w = happy_world(mk, write_error=ProfileExists("profile 'trader' already exists; nothing was changed"))
    io = ScriptedIO([*HAPPY, "back"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, w.deps)
    asked = [o for k, p, o in io.prompts if [x for x, _ in o] == ["rename", "back"]]
    assert len(asked) == 1
    said = "\n".join(io.said).lower()
    assert "already" in said and "nothing was changed" in said


def test_profile_exists_rename_returns_to_the_name_question(mk):
    w = happy_world(mk, write_error=ProfileExists("x"))
    seen, hook = w.trace()
    with pytest.raises(ScriptExhausted):
        w.run([*HAPPY, "rename"], on_prompt=hook)
    assert seen[-1] is State.PRECHECK
    assert "profile_name" not in json.loads(w.state_text())["decisions"]


def test_write_error_shows_only_the_scrubbed_report_and_never_a_secret(mk):
    def err(plan):
        return WriteError(f"setup failed after step 'config': hermes exited 1: echoed {KEY1} and {KEY2}",
                          Receipt(profile_name=plan.profile_name))
    w = happy_world(mk, write_error=err)
    receipt, io = w.run([*HAPPY, "stop"])
    assert receipt is None
    text = io.everything()
    assert_no_leak(text, KEY1, KEY2)
    assert "setup failed after step 'config'" in text
    assert WizardState.load(w.platform).current is State.REVIEW


def test_write_error_secret_in_unusual_encodings_is_still_hidden(mk):
    def err(plan):
        return WriteError(f'failed: {{"k": "{PASTED}"}}', Receipt(profile_name=plan.profile_name))
    w = mk(write_error=err)
    receipt, io = w.run([*TOP, "paste-openrouter", PASTED, "continue", "none", "done", "do", "stop"])
    assert_no_leak(io.everything(), PASTED)


def test_unexpected_value_error_from_the_writer_is_shown_scrubbed(mk):
    w = happy_world(mk, write_error=ValueError(f"bad thing {KEY1}"))
    receipt, io = w.run([*HAPPY, "stop"])
    assert receipt is None
    assert_no_leak(io.everything(), KEY1)


def test_write_error_then_review_allows_another_try(mk):
    calls = {"n": 0}

    def err(plan):
        calls["n"] += 1
        return WriteError("setup failed", Receipt(profile_name=plan.profile_name))
    w = happy_world(mk)
    w.write_error = err
    seen, hook = w.trace()
    with pytest.raises(ScriptExhausted):
        w.run([*HAPPY, "review"], on_prompt=hook)
    assert seen[-1] is State.REVIEW


# ------------------------------------------------------------------ secrets never leak (state file, say, prompts)

def test_no_secret_in_state_file_or_say_output(mk):
    w = mk(environ={**ALPACA_ENV, "OPENROUTER_API_KEY": K4}, findings=[*OLLAMA, *ALPACA_FINDINGS])
    states: list[str] = []
    answers = [*TOP, "paste-openrouter", PASTED, "continue", "pick:alpaca", "continue", True, True, "true",
               "lib:skill-keyed", "done", "add", "paste", K5, "do"]
    receipt, io = w.run(answers, on_prompt=lambda io_: states.append(w.state_text()))
    assert receipt is not None
    blob = "\n".join(states) + io.everything() + repr(receipt) + json.dumps(receipt.__dict__, default=str)
    assert_no_leak(blob, KEY1, KEY2, PASTED, K4, K5)
    sweep_tree(w.tmp, ALL_SECRETS)
    decisions = json.loads(w.state_text())["decisions"]
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in decisions.items())
    assert not [k for k, v in decisions.items() if v in ("reuse", "paste", "plain", "same")]   # consent is per process


def test_default_detectors_are_callable_and_skip_other_apps_when_told(tmp_path):
    from trading_agent.wizard.flow import default_detectors
    fp = FakePlatform(tmp_path, environ={}, which_map={})
    d = default_detectors(fp)
    assert d.mcp_files(False) == []
    assert isinstance(d.env(), list) and isinstance(d.ai_clis(), list)
