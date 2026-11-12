"""Scripted wizard runs against the shim hermes with a real temp HERMES_HOME (nothing touches ~/.hermes)."""
from __future__ import annotations

import io as _io
import json
from pathlib import Path
import stat

import pytest
import yaml
from dotenv.main import DotEnv

from fakes import K1, K2, K3, ScriptedIO, ScriptExhausted, assert_no_leak, env_finding, port_finding
from test_profile_writer import LOCAL_ROUTE, Env
from test_wizard_flow import LIBS, entry
from trading_agent.profile.receipt import Receipt
from trading_agent.wizard.flow import Deps, Detectors, run_wizard
from trading_agent.wizard.state import State, WizardState

S1, S2, S3 = K1, K2, K3
REAL_OVERLAY = json.loads((Path(__file__).parents[1] / "catalog" / "overlay.json").read_text())
REAL_TRADIER = REAL_OVERLAY["tradier"]["routes"]    # the real catalog entry, not a fixture that masks its gaps
TOP = ["start", "", "scan"]
OLLAMA = [port_finding("ollama")]


@pytest.fixture
def world(tmp_path):
    return Env(tmp_path)


def deps_for(world, *, findings=(), catalog=None, libraries=None, online=True):
    fs = list(findings)

    def pick(kinds):
        return [f for f in fs if f.kind in kinds]
    return Deps(
        platform=world.platform, hermes=world.hermes,
        detectors=Detectors(env=lambda: pick(("ai-key", "broker-key", "data-key")),
                            mcp_files=lambda enabled: pick(("mcp-entry",)) if enabled else [],
                            ai_clis=lambda: pick(("ai-cli",)), ports=lambda: pick(("ai-local", "gateway"))),
        catalog=catalog if catalog is not None else [entry("alpaca", "Alpaca", [LOCAL_ROUTE]),
                                                     entry("tradier", "Tradier", REAL_TRADIER)],
        libraries=LIBS if libraries is None else libraries,
        store_factory=world.store_factory, source_dir=world.dist, online=online)


def tree_files(world):
    return [p for p in world.root.rglob("*") if p.is_file()]


def assert_secret_only_in_profile_env(world, secrets, *, harness=()):
    """``harness``: files the test itself planted the value in (the shim's control file), not the wizard's."""
    env_file = world.pdir() / ".env"
    for p in tree_files(world):
        if p == env_file or p.name in harness:
            continue
        data = p.read_bytes()
        for s in secrets:
            assert s.encode() not in data, f"{s[:12]} found in {p.relative_to(world.root)}"


def dotenv_read(path):
    return dict(DotEnv(dotenv_path=None, stream=_io.StringIO(path.read_text()), interpolate=False).parse())


def test_e2e_local_broker_with_pasted_and_reused_keys(world):
    world.platform._e["ALPACA_API_KEY"] = S3          # found in the environment, copied only after a yes
    deps = deps_for(world, findings=[*OLLAMA, env_finding("ALPACA_API_KEY", "broker-key")])
    io = ScriptedIO([*TOP, "use:ai-local:ollama", "continue", "pick:alpaca", "continue", True, "paste", S2, "true",
                     "continue", "lib:skill-a", "done", "do"])
    receipt = run_wizard(io, deps)

    assert isinstance(receipt, Receipt) and receipt.profile_name == "trader"
    pdir = world.pdir()
    assert (pdir / "SOUL.md").is_file()
    cfg = yaml.safe_load((pdir / "config.yaml").read_text())
    assert cfg["auth"]["adopt_external_logins"] is False
    assert cfg["model"]["provider"] == "custom" and cfg["model"]["base_url"] == "http://127.0.0.1:11434/v1"
    server = cfg["mcp_servers"]["alpaca"]
    assert server["command"] == "uvx" and server["args"] == ["alpaca-mcp-server"]
    assert server["env"] == {"ALPACA_API_KEY": "${ALPACA_API_KEY}", "ALPACA_SECRET_KEY": "${ALPACA_SECRET_KEY}",
                             "ALPACA_PAPER_TRADE": "${ALPACA_PAPER_TRADE}"}
    assert dotenv_read(pdir / ".env") == {"ALPACA_API_KEY": S3, "ALPACA_SECRET_KEY": S2, "ALPACA_PAPER_TRADE": "true"}
    assert stat.S_IMODE((pdir / ".env").stat().st_mode) == 0o600
    # receipt: returned object equals the one on disk, names only
    assert Receipt.load(world.platform, "trader") == receipt
    assert receipt.env_names_written == ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER_TRADE"]
    assert receipt.skills == ["skill-a"] and receipt.broker == {"slug": "alpaca", "route_id": "local"}
    # no secret value anywhere in the tree except the profile .env (state file, receipt, hermes call log, config)
    assert_secret_only_in_profile_env(world, [S2, S3])
    assert S2 not in io.everything() and S3 not in io.everything()
    assert WizardState.load(world.platform).current is State.DONE
    assert "You are set up." in "\n".join(io.said)


def test_e2e_remote_broker_from_the_real_overlay_names_the_broker_in_every_variable(world):
    deps = deps_for(world, findings=OLLAMA)
    io = ScriptedIO([*TOP, "use:ai-local:ollama", "continue", "pick:tradier", "continue", "true", "paste", S1,
                     "continue", "done", "do"])
    receipt = run_wizard(io, deps)
    assert receipt is not None
    cfg = yaml.safe_load((world.pdir() / "config.yaml").read_text())
    assert cfg["mcp_servers"]["tradier"] == {
        "url": "https://mcp.tradier.com/mcp",
        "headers": {"API_KEY": "${TRADIER_API_KEY}", "PAPER_TRADING": "${TRADIER_PAPER_TRADING}"}}
    # no generic API_KEY in the shared .env: collisions across brokers are impossible
    assert dotenv_read(world.pdir() / ".env") == {"TRADIER_API_KEY": S1, "TRADIER_PAPER_TRADING": "true"}
    assert any(p.startswith("Tradier: ") and "TRADIER_API_KEY" in p for k, p, _ in io.prompts)
    assert_secret_only_in_profile_env(world, [S1])


def test_e2e_nothing_found_still_sets_up_a_bare_profile(world):
    io = ScriptedIO([*TOP, "skip", "continue", "none", "done", "do"])
    receipt = run_wizard(io, deps_for(world))
    assert receipt is not None
    pdir = world.pdir()
    assert yaml.safe_load((pdir / "config.yaml").read_text())["auth"]["adopt_external_logins"] is False
    assert not (pdir / ".env").exists()
    said = "\n".join(io.said)
    assert "Left to finish later:" in said and "Pick an AI" in said and "Pick a broker" in said
    assert Receipt.load(world.platform, "trader") == receipt


def test_e2e_offline_setup_completes(world):
    io = ScriptedIO([*TOP, "use:ai-local:ollama", "continue", "none", "done", "do"])
    receipt = run_wizard(io, deps_for(world, findings=OLLAMA, online=False))
    assert receipt is not None
    later = "\n".join(io.said)
    assert "Needs internet" in later[later.index("Left to finish later:"):]


def test_e2e_write_failure_rolls_back_and_never_shows_the_secret(world):
    world.set_control(fail_match="config set model.provider", fail_stderr=f"boom, you sent {S1}", fail_code=1)
    deps = deps_for(world, findings=OLLAMA)
    io = ScriptedIO([*TOP, "paste-openrouter", S1, "continue", "none", "done", "do", "stop"])
    receipt = run_wizard(io, deps)
    assert receipt is None
    assert S1 not in io.everything()
    assert "Setup did not finish" in "\n".join(io.said)
    assert not world.pdir().exists()                  # rolled back
    assert not world.receipt_path().exists()
    assert_secret_only_in_profile_env(world, [S1], harness=("control.json",))
    assert WizardState.load(world.platform).current is State.REVIEW


def test_e2e_write_failure_then_retry_succeeds(world):
    world.set_control(fail_match="config set auth", fail_stderr="transient", fail_code=1)

    def heal(io):
        # the control file is read on every shim call: heal it while the failure prompt is open
        if io.prompts and io.prompts[-1][1] == "What now?":
            world.set_control()
    deps = deps_for(world, findings=OLLAMA)
    io = ScriptedIO([*TOP, "use:ai-local:ollama", "continue", "none", "done", "do", "review", "do"],
                    on_prompt=heal)
    receipt = run_wizard(io, deps)
    assert receipt is not None and world.pdir().is_dir()


def test_e2e_resume_reasks_the_pasted_value_and_finishes(world):
    deps = deps_for(world, findings=OLLAMA)
    first = ScriptedIO([*TOP, "paste-openrouter", S1, "continue", "none", "done"])
    with pytest.raises(ScriptExhausted):
        run_wizard(first, deps)                       # interrupted at REVIEW, nothing written yet
    assert not world.pdir().exists()
    state_text = (world.config_home / "trading-agent" / "state.json").read_text()
    assert S1 not in state_text
    second = ScriptedIO(["paste", S2, "do"])                   # a fresh process: the value must be pasted again
    receipt = run_wizard(second, deps_for(world, findings=OLLAMA))
    assert receipt is not None
    assert dotenv_read(world.pdir() / ".env") == {"OPENROUTER_API_KEY": S2}
    assert_secret_only_in_profile_env(world, [S1, S2])


def test_e2e_existing_profile_is_never_overwritten(world):
    world.pdir().mkdir(parents=True)
    (world.pdir() / "keep.txt").write_text("mine")
    io = ScriptedIO(["start", "", "back"])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, deps_for(world))               # "You already have a profile": rename or Back only
    assert [i for i, _ in io.prompts[-2][2]] == ["rename", "back"]
    assert (world.pdir() / "keep.txt").read_text() == "mine"
    assert not any("install" in " ".join(c) for c in world.calls())


def test_e2e_state_file_and_everything_said_never_hold_a_value(world):
    world.platform._e["OPENROUTER_API_KEY"] = S3
    deps = deps_for(world, findings=[env_finding("OPENROUTER_API_KEY")])
    seen_states = []

    def hook(io_):
        p = world.config_home / "trading-agent" / "state.json"
        seen_states.append(p.read_text() if p.exists() else "")
    io = ScriptedIO([*TOP, "use:env:OPENROUTER_API_KEY", True, "continue", "none", "done", "do"], on_prompt=hook)
    assert run_wizard(io, deps) is not None
    for text in seen_states + [io.everything()]:
        assert_no_leak(text, S3)
    assert dotenv_read(world.pdir() / ".env") == {"OPENROUTER_API_KEY": S3}


def test_e2e_a_stale_receipt_never_makes_a_false_you_are_set_up(world):
    import shutil
    deps = deps_for(world, findings=OLLAMA)
    first = run_wizard(ScriptedIO([*TOP, "use:ai-local:ollama", "continue", "none", "done", "do"]), deps)
    assert first is not None and world.receipt_path().exists()
    shutil.rmtree(world.pdir())                              # the profile was deleted; its receipt file is left over
    state_path = world.config_home / "trading-agent" / "state.json"
    doc = json.loads(state_path.read_text())
    doc["current"] = "WRITE"                                 # an interrupted second run, resumed at WRITE
    state_path.write_text(json.dumps(doc))
    io = ScriptedIO([])
    with pytest.raises(ScriptExhausted):
        run_wizard(io, deps_for(world, findings=OLLAMA))
    assert "You are set up" not in "\n".join(io.said)
    assert WizardState.load(world.platform).current is State.REVIEW
    # a profile recreated by someone else (no ownership marker) is no better proof
    world.pdir().mkdir(parents=True)
    doc["current"] = "VERIFY"
    state_path.write_text(json.dumps(doc))
    io2 = ScriptedIO([])
    with pytest.raises(ScriptExhausted):
        run_wizard(io2, deps_for(world, findings=OLLAMA))
    assert "You are set up" not in "\n".join(io2.said)
