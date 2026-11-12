from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

import pytest
import yaml
from dotenv.main import DotEnv

from fakes import FakePlatform
from trading_agent.platform import LinuxPlatform, RunResult
from trading_agent.profile import receipt as receipt_mod
from trading_agent.profile.hermes_cli import ForbiddenHermesArgs, HermesCli, validate_profile_name
from trading_agent.profile.receipt import Receipt
from trading_agent.profile.writer import (
    AiChoice, BrokerChoice, ProfileExists, WriteError, WritePlan, build_mcp_entry, rollback, write_profile,
)
from trading_agent.secrets import HIDDEN, EnvFileStore, choose_store

SHIM = Path(__file__).parent / "fixtures" / "hermes_shim.py"
SECRET = "sk-TESTSECRET-0123456789abcdef"

LOCAL_ROUTE = {
    "route_id": "local", "kind": "local-command",
    "install": {"method": "uvx", "command_text": "uvx alpaca-mcp-server"},
    "auth": {"type": "api-key-plus-secret", "env_vars": [
        {"name": "ALPACA_API_KEY", "role": "key-id", "required": True, "secret": True},
        {"name": "ALPACA_SECRET_KEY", "role": "secret", "required": True, "secret": True},
        {"name": "ALPACA_PAPER_TRADE", "role": "mode-switch", "required": False, "secret": False},
    ]},
}
REMOTE_ROUTE = {
    "route_id": "hosted", "kind": "remote-url", "url": "https://mcp.tradier.com/mcp",
    "auth": {"type": "bearer-token", "env_vars": [], "headers": ["API_KEY", "PAPER_TRADING"],
             "header_env": {"API_KEY": "TRADIER_API_KEY"}},
}


class TmpPlatform(LinuxPlatform):
    """Real process spawning and real files, rooted in a temp dir."""

    def __init__(self, home, config_home, environ):
        self._h, self._c, self._e = Path(home), Path(config_home), dict(environ)

    def home(self): return self._h
    def config_home(self): return self._c
    def environ(self): return self._e
    def which(self, name):
        found = shutil.which(name, path=self._e.get("PATH", ""))
        return Path(found) if found else None


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.hermes_home = root / "world" / "hermes_home"
        self.config_home = root / "world" / "config"
        self.dist = root / "dist"
        self.ctl_dir = root / "ctl"
        for d in (self.hermes_home / "profiles", self.config_home, self.dist, self.ctl_dir, root / "bin"):
            d.mkdir(parents=True, exist_ok=True)
        (self.dist / "distribution.yaml").write_text("name: shim\nversion: 0.1.0\n")
        (self.dist / "SOUL.md").write_text("soul\n")
        (self.dist / "config.yaml").write_text("model: {}\n")
        self.log = self.ctl_dir / "calls.jsonl"
        self.control = self.ctl_dir / "control.json"
        exe = root / "bin" / "hermes"
        exe.write_text(
            f"#!{sys.executable}\nimport os, runpy, sys\n"
            f"os.environ['SHIM_LOG'] = {str(self.log)!r}\nos.environ['SHIM_CONTROL'] = {str(self.control)!r}\n"
            f"sys.argv[0] = {str(SHIM)!r}\nrunpy.run_path({str(SHIM)!r}, run_name='__main__')\n")
        exe.chmod(0o755)
        self.platform = TmpPlatform(root / "world" / "home", self.config_home, {
            "PATH": f"{root / 'bin'}:{os.environ.get('PATH', '')}", "HERMES_HOME": str(self.hermes_home),
            "HOME": str(root / "world" / "home"),
            "SOME_PROVIDER_KEY": "must-not-be-forwarded",
        })
        self.hermes = HermesCli(self.platform)

    def set_control(self, **kw):
        self.control.write_text(json.dumps(kw))

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def store_factory(self, profile_dir):
        return choose_store(self.platform, profile_dir)

    def pdir(self, name="trader") -> Path:
        return self.hermes_home / "profiles" / name

    def receipt_path(self, name="trader") -> Path:
        return self.config_home / "trading-agent" / "receipts" / f"{name}.json"

    def snapshot(self) -> dict[str, bytes | None]:
        out: dict[str, bytes | None] = {}
        for base in (self.hermes_home, self.config_home):
            for p in base.rglob("*"):
                out[str(p)] = None if p.is_dir() else p.read_bytes()
        return out


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def plan(name="trader", ai=None, broker=None, skills=(), env_values=None):
    return WritePlan(profile_name=name, ai=ai, broker=broker, skills=list(skills),
                     env_values=dict(env_values or {}))


def run_write(env, p):
    return write_profile(p, hermes=env.hermes, source_dir=env.dist, store_factory=env.store_factory)


def local_broker():
    return BrokerChoice(slug="alpaca", route_id="local", entry=LOCAL_ROUTE)


def remote_broker():
    return BrokerChoice(slug="tradier", route_id="hosted", entry=REMOTE_ROUTE)


def read_cfg(env, name="trader"):
    return yaml.safe_load((env.pdir(name) / "config.yaml").read_text())


def dotenv_read(path: Path) -> dict:
    text = path.read_bytes().decode("utf-8-sig")
    return dict(DotEnv(dotenv_path=None, stream=io.StringIO(text), interpolate=False).parse())


# ---------------------------------------------------------------- HermesCli

FORBIDDEN = [
    ["profile", "install", "d", "--name", "n", "-y", "--clone"],
    ["profile", "create", "n", "--clone-all"],
    ["profile", "create", "n", "--clone-from", "other"],
    ["profile", "create", "n", "--clone-from=other"],
    ["mcp", "add", "x", "--env", "K=V"],
    ["mcp", "add", "x", "--env=K=V"],
    ["auth", "list"],
    ["-p", "n", "auth", "status"],
    ["import-agent"],
    ["-p", "n", "import-agent", "--from", "x"],
]


@pytest.mark.parametrize("args", FORBIDDEN)
def test_forbidden_hermes_args(tmp_path, args):
    fp = FakePlatform(tmp_path, which_map={"hermes": "/bin/hermes"})
    with pytest.raises(ForbiddenHermesArgs):
        HermesCli(fp).run(args, timeout=5)
    assert fp.run_calls == []


def test_forbidden_also_with_binary_prefix(tmp_path):
    fp = FakePlatform(tmp_path)
    with pytest.raises(ForbiddenHermesArgs):
        HermesCli(fp).run(["hermes", "auth", "list"], timeout=5)
    assert fp.run_calls == []


def test_cli_env_and_timeouts(tmp_path):
    fp = FakePlatform(tmp_path, environ={"HERMES_HOME": str(tmp_path / "hh"), "PATH": "/usr/bin",
                                         "HOME": "/h", "OPENROUTER_API_KEY": "nope"},
                      which_map={"hermes": "/opt/hermes"})
    cli = HermesCli(fp)
    cli.profile_install(tmp_path / "d", "trader")
    cli.config_set("trader", "auth.adopt_external_logins", "false")
    cli.profile_update("trader")
    cli.profile_delete("trader")
    assert fp.run_calls[0] == ["/opt/hermes", "profile", "install", str(tmp_path / "d"), "--name", "trader", "-y"]
    assert fp.run_calls[1] == ["/opt/hermes", "-p", "trader", "config", "set", "auth.adopt_external_logins", "false"]
    assert fp.run_calls[2] == ["/opt/hermes", "profile", "update", "trader", "-y"]
    assert fp.run_calls[3] == ["/opt/hermes", "profile", "delete", "trader", "-y"]
    assert fp.run_timeouts == [120, 30, 30, 30]
    for e in fp.run_envs:
        assert e["HERMES_HOME"] == str(tmp_path / "hh")
        assert "OPENROUTER_API_KEY" not in e


def test_cli_env_without_hermes_home_sets_none(tmp_path):
    fp = FakePlatform(tmp_path, environ={"PATH": "/usr/bin"}, which_map={"hermes": "/opt/hermes"})
    HermesCli(fp).run(["profile", "list"], timeout=30)
    assert "HERMES_HOME" not in fp.run_envs[0]


def test_profile_dir_rule(tmp_path):
    fp = FakePlatform(tmp_path / "h", environ={"HERMES_HOME": str(tmp_path / "hh")})
    assert HermesCli(fp).profile_dir("trader") == tmp_path / "hh" / "profiles" / "trader"
    fp2 = FakePlatform(tmp_path / "h")
    assert HermesCli(fp2).profile_dir("trader") == tmp_path / "h" / ".hermes" / "profiles" / "trader"


def test_version_and_exists_with_shim(env):
    assert env.hermes.version().startswith("Hermes Agent v0.23.1")
    assert not env.hermes.profile_exists("trader")
    env.pdir().mkdir(parents=True)
    assert env.hermes.profile_exists("trader")
    assert not env.hermes.profile_exists("other")


def test_exists_true_when_only_listed(env):
    env.set_control(listed=["trader"])
    assert env.hermes.profile_exists("trader")


@pytest.mark.parametrize("bad", ["hermes", "default", "test", "tmp", "root", "sudo", "", "Trader", "1abc",
                                 "a b", "../x", "a/b", "-x", "x" * 33, "tr\nader", "t$r"])
def test_invalid_and_reserved_names_value_free(env, bad):
    with pytest.raises(ValueError) as e:
        validate_profile_name(bad)
    if bad:
        assert bad not in str(e.value)
    with pytest.raises(ValueError):
        run_write(env, plan(name=bad))
    assert env.calls() == []
    assert not env.receipt_path().exists()


@pytest.mark.parametrize("good", ["trader", "a", "my-agent_2", "x" * 32])
def test_valid_names(good):
    validate_profile_name(good)


# ---------------------------------------------------------------- write path

def test_refuses_existing_profile_and_calls_no_write_step(env):
    env.pdir().mkdir(parents=True)
    (env.pdir() / "keep.txt").write_text("mine")
    before = env.snapshot()
    with pytest.raises(ProfileExists):
        run_write(env, plan(env_values={"K": "v"}, broker=local_broker()))
    assert env.snapshot() == before
    verbs = [" ".join(c) for c in env.calls()]
    assert not any("install" in v or "config set" in v or "delete" in v for v in verbs)
    assert not env.receipt_path().exists()


def test_refuses_profile_known_only_to_hermes_list(env):
    env.set_control(listed=["trader"])
    with pytest.raises(ProfileExists):
        run_write(env, plan())
    assert not any("install" in " ".join(c) for c in env.calls())


def test_happy_path_minimal(env):
    r = run_write(env, plan(ai=AiChoice("none", ""), skills=["alpha-skills"]))
    assert r.profile_name == "trader"
    assert (env.pdir() / "SOUL.md").read_text() == "soul\n"
    cfg = read_cfg(env)
    assert cfg["auth"] == {"adopt_external_logins": False}
    assert "mcp_servers" not in cfg
    assert not (env.pdir() / ".env").exists()
    assert r.steps_done == ["install", "config", "broker", "secrets", "skills"]
    saved = json.loads(env.receipt_path().read_text())
    assert saved["profile_name"] == "trader" and saved["skills"] == ["alpha-skills"]
    assert str(env.pdir()) in saved["created_paths"]
    assert stat.S_IMODE(env.receipt_path().stat().st_mode) == 0o600
    # no skill installation, no messaging, no forbidden verbs
    for c in env.calls():
        assert "skills" not in c and "gateway" not in c and "mcp" not in c


def test_mcp_entry_uses_references_not_values_stdio(env):
    values = {"ALPACA_API_KEY": SECRET, "ALPACA_SECRET_KEY": SECRET + "-s", "ALPACA_PAPER_TRADE": "true"}
    run_write(env, plan(broker=local_broker(), env_values=values))
    text = (env.pdir() / "config.yaml").read_text()
    assert "${ALPACA_API_KEY}" in text
    assert SECRET not in text
    entry = read_cfg(env)["mcp_servers"]["alpaca"]
    assert entry["command"] == "uvx" and entry["args"] == ["alpaca-mcp-server"]
    assert entry["env"] == {"ALPACA_API_KEY": "${ALPACA_API_KEY}", "ALPACA_SECRET_KEY": "${ALPACA_SECRET_KEY}",
                            "ALPACA_PAPER_TRADE": "${ALPACA_PAPER_TRADE}"}
    envfile = dotenv_read(env.pdir() / ".env")
    assert envfile["ALPACA_API_KEY"] == SECRET and envfile["ALPACA_SECRET_KEY"] == SECRET + "-s"
    assert not (env.pdir() / "mcp.json").exists()


def test_stdio_secret_listed_even_when_value_not_supplied(env):
    run_write(env, plan(broker=local_broker(), env_values={}))
    entry = read_cfg(env)["mcp_servers"]["alpaca"]
    assert entry["env"] == {"ALPACA_API_KEY": "${ALPACA_API_KEY}", "ALPACA_SECRET_KEY": "${ALPACA_SECRET_KEY}"}


def test_mcp_entry_remote_headers_use_references(env):
    run_write(env, plan(broker=remote_broker(),
                        env_values={"TRADIER_API_KEY": SECRET, "PAPER_TRADING": "true"}))
    text = (env.pdir() / "config.yaml").read_text()
    assert SECRET not in text
    entry = read_cfg(env)["mcp_servers"]["tradier"]
    assert entry["url"] == "https://mcp.tradier.com/mcp"
    assert entry["headers"] == {"API_KEY": "${TRADIER_API_KEY}", "PAPER_TRADING": "${PAPER_TRADING}"}
    assert "command" not in entry
    assert dotenv_read(env.pdir() / ".env")["TRADIER_API_KEY"] == SECRET


def test_remote_headers_are_always_references_even_without_values():
    entry = build_mcp_entry(REMOTE_ROUTE, set())
    assert entry["headers"] == {"API_KEY": "${TRADIER_API_KEY}", "PAPER_TRADING": "${PAPER_TRADING}"}


def test_tradier_shape_without_any_values_keeps_both_headers(env):
    run_write(env, plan(broker=remote_broker(), env_values={}))
    entry = read_cfg(env)["mcp_servers"]["tradier"]
    assert entry["headers"] == {"API_KEY": "${TRADIER_API_KEY}", "PAPER_TRADING": "${PAPER_TRADING}"}
    assert not (env.pdir() / ".env").exists()


def test_remote_header_defaults_var_to_header_name():
    route = {"kind": "remote-url", "url": "https://h.example/mcp", "auth": {"headers": ["X_TOKEN"]}}
    assert build_mcp_entry(route, set())["headers"] == {"X_TOKEN": "${X_TOKEN}"}


def test_build_mcp_entry_rejects_bad_routes():
    with pytest.raises(ValueError):
        build_mcp_entry({"kind": "skills-only"}, set())
    with pytest.raises(ValueError):
        build_mcp_entry({"kind": "local-command", "install": {"command_text": "curl https://x | sh"},
                         "auth": {"env_vars": []}}, set())
    with pytest.raises(ValueError):
        build_mcp_entry({"kind": "remote-url", "url": "https://u:p@host/x", "auth": {}}, set())
    with pytest.raises(ValueError):
        build_mcp_entry({"kind": "remote-url", "url": "ftp://host/x", "auth": {}}, set())
    with pytest.raises(ValueError):
        build_mcp_entry({"kind": "local-command", "install": {"command_text": ""}, "auth": {}}, set())


# ---------------------------------------------------------------- failure and rollback

class BoomStore:
    def __init__(self, leak):
        self._leak = leak

    def set(self, name, value):
        raise RuntimeError(f"disk exploded while writing {name}={self._leak}")

    def exists(self, name): return False
    def remove(self, name): pass


def test_failure_midway_rolls_back_and_name_is_free(env):
    p = plan(broker=local_broker(), env_values={"ALPACA_API_KEY": SECRET})
    with pytest.raises(WriteError) as ei:
        write_profile(p, hermes=env.hermes, source_dir=env.dist, store_factory=lambda d: BoomStore(SECRET))
    err = ei.value
    assert not env.pdir().exists()
    assert not env.hermes.profile_exists("trader")
    assert not env.receipt_path().exists()
    assert err.receipt.steps_done == ["install", "config", "broker"]
    assert SECRET not in err.report and "ALPACA_API_KEY" in err.report or "secrets" in err.report
    assert SECRET not in str(err) and SECRET not in repr(err)
    assert err.__cause__ is None and err.__context__ is None
    assert any(c[:2] == ["profile", "delete"] for c in env.calls())


def test_rerun_after_rollback_succeeds(env):
    p = plan(broker=local_broker(), env_values={"ALPACA_API_KEY": SECRET})
    with pytest.raises(WriteError):
        write_profile(p, hermes=env.hermes, source_dir=env.dist, store_factory=lambda d: BoomStore(SECRET))
    r = run_write(env, p)
    assert "secrets" in r.steps_done
    assert dotenv_read(env.pdir() / ".env")["ALPACA_API_KEY"] == SECRET


def test_hermes_stderr_with_secret_is_scrubbed(env):
    tricky = 'p"w #$ x'
    env.set_control(fail_match="config set model.provider", fail_code=1,
                    fail_stderr=f"bad value {SECRET} and {tricky!r} and json {json.dumps(tricky)}\n",
                    fail_stdout=f"stdout echo {SECRET}\n")
    p = plan(ai=AiChoice("openrouter", ""), env_values={"OPENROUTER_API_KEY": SECRET, "OTHER": tricky})
    with pytest.raises(WriteError) as ei:
        run_write(env, p)
    rep = ei.value.report
    assert SECRET not in rep and tricky not in rep and json.dumps(tricky)[1:-1] not in rep
    assert HIDDEN in rep
    assert not env.pdir().exists()
    assert not env.receipt_path().exists()
    assert SECRET not in str(ei.value)


def test_pattern_redaction_catches_unknown_secret_in_report(env):
    env.set_control(fail_match="config set auth", fail_stderr="token sk-NOTAKNOWNVALUE123456 leaked\n")
    with pytest.raises(WriteError) as ei:
        run_write(env, plan())
    assert "sk-NOTAKNOWNVALUE123456" not in ei.value.report
    assert not env.pdir().exists()


def test_install_exit0_but_no_profile_is_failure(env):
    env.set_control(cancel_install=True)
    with pytest.raises(WriteError) as ei:
        run_write(env, plan())
    assert "install" in ei.value.report
    assert not env.pdir().exists()
    assert not env.receipt_path().exists()
    assert not any(c[:2] == ["profile", "delete"] for c in env.calls())
    assert ei.value.receipt.steps_done == []


def test_install_failure_never_deletes_what_it_did_not_create(env):
    env.set_control(fail_match="profile install", fail_stdout="Error: Profile 'trader' already exists at x.\n")
    with pytest.raises(WriteError):
        run_write(env, plan())
    assert not any(c[:2] == ["profile", "delete"] for c in env.calls())


def test_config_failure_after_install_rolls_back(env):
    env.set_control(fail_match="config set auth.adopt_external_logins", fail_stderr="nope\n")
    with pytest.raises(WriteError) as ei:
        run_write(env, plan())
    assert not env.pdir().exists() and not env.receipt_path().exists()
    assert ei.value.receipt.steps_done == ["install"]


def test_receipt_saved_after_each_step_and_holds_no_value(env):
    seen = []

    def factory(pdir):
        seen.append(json.loads(env.receipt_path().read_text()))
        return env.store_factory(pdir)

    r = write_profile(plan(ai=AiChoice("openrouter", ""), broker=local_broker(), skills=["s1"],
                           env_values={"ALPACA_API_KEY": SECRET, "OPENROUTER_API_KEY": SECRET + "2"}),
                      hermes=env.hermes, source_dir=env.dist, store_factory=factory)
    assert seen[0]["steps_done"] == ["install", "config", "broker"]
    assert seen[0]["env_names_written"] == []
    final = json.loads(env.receipt_path().read_text())
    assert final["steps_done"] == ["install", "config", "broker", "secrets", "skills"]
    assert final["env_names_written"] == ["ALPACA_API_KEY", "OPENROUTER_API_KEY"]
    assert final["broker"] == {"slug": "alpaca", "route_id": "local"}
    assert final["ai_choice"] == {"kind": "openrouter", "ref": ""}
    assert SECRET not in env.receipt_path().read_text()
    assert r.steps_done == final["steps_done"]
    assert Receipt.load(env.platform, "trader").profile_name == "trader"


def test_receipt_survives_a_crash_between_steps(env):
    class Crash(BaseException):
        pass

    def factory(pdir):
        raise Crash()

    with pytest.raises(Crash):
        write_profile(plan(env_values={"A_KEY": "v"}), hermes=env.hermes, source_dir=env.dist,
                      store_factory=factory)
    # BaseException: rolled back too, nothing left behind
    assert not env.pdir().exists()


def test_writes_only_into_new_profile(env):
    pre = env.snapshot()
    other = env.hermes_home / "profiles" / "other"
    other.mkdir()
    (other / "f").write_text("x")
    (env.hermes_home / "config.yaml").write_text("global: 1\n")
    pre = env.snapshot()
    run_write(env, plan(ai=AiChoice("custom", "https://api.venice.ai/api/v1", "VENICE_API_KEY"),
                        broker=local_broker(), skills=["a"],
                        env_values={"ALPACA_API_KEY": SECRET, "VENICE_API_KEY": SECRET + "v"}))
    post = env.snapshot()
    pdir = str(env.pdir())
    rpath = str(env.receipt_path())
    receipt_dir = str(env.receipt_path().parent)
    receipt_root = str(env.receipt_path().parent.parent)
    changed = {k for k in set(pre) | set(post) if pre.get(k, "ABSENT") != post.get(k, "ABSENT")}
    assert changed
    for k in changed:
        assert k == pdir or k.startswith(pdir + os.sep) or k == rpath or k == receipt_dir or k == receipt_root, k
    assert (env.hermes_home / "config.yaml").read_text() == "global: 1\n"
    assert (other / "f").read_text() == "x"


# ---------------------------------------------------------------- providers

def calls_config_set(env):
    return [c for c in env.calls() if c[:1] == ["-p"] and c[2:4] == ["config", "set"]]


LOCAL = lambda url: ({"provider": "custom", "base_url": url}, None)  # noqa: E731


@pytest.mark.parametrize("ai,model,providers,keys", [
    (AiChoice("none", ""), {}, None, []),
    (AiChoice("openrouter", ""), {"provider": "openrouter"}, None, ["model.provider"]),
    (AiChoice("custom", "https://api.venice.ai/api/v1", "VENICE_API_KEY"),
     {"provider": "custom_endpoint"},
     {"custom_endpoint": {"name": "custom_endpoint", "base_url": "https://api.venice.ai/api/v1",
                          "key_env": "VENICE_API_KEY"}},
     ["model.provider", "providers.custom_endpoint.name", "providers.custom_endpoint.base_url",
      "providers.custom_endpoint.key_env"]),
    (AiChoice("custom", "https://api.openai.com/v1", ""),
     {"provider": "custom_endpoint"},
     {"custom_endpoint": {"name": "custom_endpoint", "base_url": "https://api.openai.com/v1"}},
     ["model.provider", "providers.custom_endpoint.name", "providers.custom_endpoint.base_url"]),
    (AiChoice("ollama", ""), {"provider": "custom", "base_url": "http://127.0.0.1:11434/v1"}, None,
     ["model.provider", "model.base_url"]),
    (AiChoice("lm-studio", ""), {"provider": "custom", "base_url": "http://127.0.0.1:1234/v1"}, None,
     ["model.provider", "model.base_url"]),
])
def test_provider_config_keys(env, ai, model, providers, keys):
    run_write(env, plan(ai=ai))
    cfg = read_cfg(env)
    assert (cfg.get("model") or {}) == model
    assert cfg.get("providers") == providers
    assert "key_env" not in (cfg.get("model") or {})
    got = [c[4] for c in calls_config_set(env)]
    assert got[0] == "auth.adopt_external_logins"
    assert got[1:] == keys
    assert cfg["auth"]["adopt_external_logins"] is False


def test_cli_kind_writes_no_provider_config_but_is_recorded(env):
    run_write(env, plan(ai=AiChoice("cli", "claude")))
    cfg = read_cfg(env)
    assert cfg.get("model") in ({}, None)
    assert [c[4] for c in calls_config_set(env)] == ["auth.adopt_external_logins"]
    saved = json.loads(env.receipt_path().read_text())
    assert saved["ai_choice"] == {"kind": "cli", "ref": "claude"}


@pytest.mark.parametrize("ai", [
    AiChoice("custom", "", ""), AiChoice("custom", "ftp://x/v1", ""),
    AiChoice("custom", "https://u:p@host/v1", ""), AiChoice("custom", "https://h/v1", "lower"),
    AiChoice("custom", "https://h/v1 --flag", ""), AiChoice("openrouter", "x"),
    AiChoice("cli", ""), AiChoice("cli", "a b"), AiChoice("nope", ""),
])
def test_invalid_ai_choice_rejected_before_anything_runs(env, ai):
    with pytest.raises(ValueError):
        run_write(env, plan(ai=ai))
    assert env.calls() == []


def test_invalid_env_value_rejected_before_install(env):
    with pytest.raises(ValueError) as e:
        run_write(env, plan(env_values={"A_KEY": "x${HOME}y"}))
    assert "x${HOME}y" not in str(e.value)
    assert env.calls() == []


# ---------------------------------------------------------------- special characters

SPECIAL = ['a"b', "x#y", "p w", "$HOME", "line1\nline2", "back\\slash", "it's", "tab\tcr\r", " lead ",
           "a\\nb", "q=\"#'", "unié☃"]


@pytest.mark.parametrize("raw", SPECIAL)
def test_special_secret_lands_only_in_env_file(env, raw):
    value = f"Zk7{raw}Zk7"
    run_write(env, plan(ai=AiChoice("openrouter", ""), broker=local_broker(),
                        env_values={"ALPACA_API_KEY": value, "OPENROUTER_API_KEY": SECRET}))
    envfile = env.pdir() / ".env"
    assert dotenv_read(envfile)["ALPACA_API_KEY"] == value
    needle = value.encode()
    for base in (env.root,):
        for p in base.rglob("*"):
            if p.is_file() and p != envfile and p.name != "pyvenv.cfg":
                assert needle not in p.read_bytes(), p
    assert stat.S_IMODE(envfile.stat().st_mode) == 0o600


# ---------------------------------------------------------------- rollback safety

def test_rollback_twice_harmless_and_never_deletes_outside(env):
    sibling = env.pdir("other")
    sibling.mkdir()
    (sibling / "keep").write_text("sibling")
    outside = env.root / "outside.txt"
    outside.write_text("outside")
    outside_dir = env.root / "outdir"
    outside_dir.mkdir()
    (outside_dir / "f").write_text("f")
    r = run_write(env, plan(broker=local_broker(), env_values={"ALPACA_API_KEY": SECRET}))
    link = env.pdir() / "evil-link"
    link.symlink_to(outside)
    dlink = env.pdir() / "evil-dirlink"
    dlink.symlink_to(outside_dir, target_is_directory=True)
    r.created_paths += [str(sibling), str(outside), str(link), str(dlink / "f"), str(env.hermes_home),
                        str(env.hermes_home / "profiles"), "/", str(env.pdir() / ".." / "other")]
    rollback(r, hermes=env.hermes, store_factory=env.store_factory)
    assert not env.pdir().exists()
    assert (sibling / "keep").read_text() == "sibling"
    assert outside.read_text() == "outside" and (outside_dir / "f").read_text() == "f"
    assert env.hermes_home.is_dir() and (env.hermes_home / "profiles").is_dir()
    assert not env.receipt_path().exists()
    rollback(r, hermes=env.hermes, store_factory=env.store_factory)  # second time
    assert (sibling / "keep").read_text() == "sibling"


def test_rollback_removes_only_written_env_names(env):
    r = run_write(env, plan(env_values={"A_KEY": "1", "B_KEY": "2"}))
    removed = []

    class Spy:
        def __init__(self, inner): self._i = inner
        def set(self, n, v): self._i.set(n, v)
        def exists(self, n): return self._i.exists(n)
        def remove(self, n):
            removed.append(n)
            self._i.remove(n)

    partial = Receipt("trader", [], ["A_KEY"], ["install", "secrets"], run_id=r.run_id)
    rollback(partial, hermes=env.hermes, store_factory=lambda d: Spy(env.store_factory(d)))
    assert removed == ["A_KEY"]
    assert not env.pdir().exists()


def test_rollback_without_install_step_touches_nothing(env):
    env.pdir().mkdir(parents=True)
    (env.pdir() / "mine").write_text("x")
    rollback(Receipt("trader", [str(env.pdir())], [], []), hermes=env.hermes, store_factory=env.store_factory)
    assert (env.pdir() / "mine").exists()
    assert not any(c[:2] == ["profile", "delete"] for c in env.calls())


def test_rollback_falls_back_when_hermes_delete_fails(env):
    r = run_write(env, plan())
    env.set_control(fail_match="profile delete", fail_stderr="cannot delete\n")
    rollback(r, hermes=env.hermes, store_factory=env.store_factory)
    assert not env.pdir().exists()


def test_rollback_refuses_unsafe_name(env):
    with pytest.raises(ValueError):
        rollback(Receipt("../x", [], [], ["install"]), hermes=env.hermes, store_factory=env.store_factory)


def test_two_runs_for_different_names_keep_both_receipts(env):
    ra = run_write(env, plan(name="alpha"))
    rb = run_write(env, plan(name="beta"))
    assert Receipt.list_receipts(env.platform) == ["alpha", "beta"]
    assert env.receipt_path("alpha").exists() and env.receipt_path("beta").exists()
    assert stat.S_IMODE(env.receipt_path("alpha").stat().st_mode) == 0o600
    assert stat.S_IMODE(env.receipt_path("alpha").parent.stat().st_mode) == 0o700
    rollback(ra, hermes=env.hermes, store_factory=env.store_factory)
    assert Receipt.list_receipts(env.platform) == ["beta"]
    assert env.pdir("beta").is_dir() and not env.pdir("alpha").exists()


def test_retry_under_new_name_after_exists_keeps_earlier_receipt(env):
    run_write(env, plan(name="first"))
    with pytest.raises(ProfileExists):
        run_write(env, plan(name="first"))
    run_write(env, plan(name="second"))
    assert Receipt.list_receipts(env.platform) == ["first", "second"]


def test_receipt_roundtrip_and_validation(env):
    r = Receipt("trader", ["/a"], ["K"], ["install"], ai_choice={"kind": "none", "ref": ""},
                broker={"slug": "x", "route_id": "y"}, skills=["s"], run_id="abc")
    r.save(env.platform)
    assert Receipt.load(env.platform, "trader") == r
    env.receipt_path().write_text("{not json")
    with pytest.raises(receipt_mod.ReceiptError):
        Receipt.load(env.platform, "trader")
    env.receipt_path().write_text(json.dumps({"profile_name": 1}))
    with pytest.raises(receipt_mod.ReceiptError):
        Receipt.load(env.platform, "trader")
    env.receipt_path().unlink()
    assert Receipt.load(env.platform, "trader") is None
    with pytest.raises(ValueError):
        Receipt.load(env.platform, "../evil")


def test_receipt_save_is_atomic_no_tmp_left(env):
    Receipt("trader", [], [], []).save(env.platform)
    assert [p.name for p in env.receipt_path().parent.iterdir()] == ["trader.json"]


# ---------------------------------------------------------------- C1: scrub before normalizing

LEAKY = {
    "newline": "AAAA1111\nBBBB2222\nCCCC3333",
    "tab": "AAAA1111\tBBBB2222",
    "double-space": "AAAA1111  BBBB2222   CCCC",
    "cr": "AAAA1111\rBBBB2222\r\nCCCC3333",
    "long": "Qx9" + "abcdefghij" * 71,                      # 713 chars, past the 600 limit
    "straddle": "Zz7" + "klmnopqrst" * 20,                  # begins before the cut, ends after it
}


def _echo_variants(value):
    return [value, repr(value), json.dumps(value)]


@pytest.mark.parametrize("kind", list(LEAKY))
@pytest.mark.parametrize("stream", ["stderr", "stdout", "both"])
def test_report_never_contains_any_part_of_a_secret(env, kind, stream):
    value = LEAKY[kind]
    filler = "x " * 280 if kind == "straddle" else ""       # pushes the value across the 600 char boundary
    text = filler + " ".join(_echo_variants(value)) + " tail"
    kw = {}
    if stream in ("stderr", "both"):
        kw["fail_stderr"] = text
    if stream in ("stdout", "both"):
        kw["fail_stdout"] = text
    env.set_control(fail_match="config set model.provider", **kw)
    with pytest.raises(WriteError) as ei:
        run_write(env, plan(ai=AiChoice("openrouter", ""), env_values={"OPENROUTER_API_KEY": value}))
    rep = ei.value.report
    flat = " ".join(rep.split())
    for variant in (value, " ".join(value.split())):
        for i in range(0, max(1, len(variant) - 7)):
            assert variant[i:i + 8] not in rep and variant[i:i + 8] not in flat, (kind, stream, i)
    assert HIDDEN in rep
    assert not env.pdir().exists()


def test_exception_text_with_secret_is_scrubbed_too(env):
    value = "Ab1\n" + "k" * 700
    with pytest.raises(WriteError) as ei:
        write_profile(plan(env_values={"A_KEY": value}), hermes=env.hermes, source_dir=env.dist,
                      store_factory=lambda d: BoomStore(value))
    for i in range(0, len(value) - 7, 7):
        assert value[i:i + 8] not in ei.value.report


# ---------------------------------------------------------------- I1: ownership marker

def test_marker_written_0600_with_run_id_and_in_receipt(env):
    r = run_write(env, plan())
    marker = env.pdir() / ".trading-agent-run"
    assert r.run_id and len(r.run_id) == 32
    assert marker.read_text().strip() == r.run_id
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert json.loads(env.receipt_path().read_text())["run_id"] == r.run_id


def test_old_receipt_cannot_delete_a_recreated_profile(env):
    old = run_write(env, plan())
    rollback(old, hermes=env.hermes, store_factory=env.store_factory)
    assert not env.pdir().exists()
    new = run_write(env, plan())
    (env.pdir() / "user-work.txt").write_text("precious")
    assert old.run_id != new.run_id
    stale = Receipt("trader", [str(env.pdir())], [], ["install", "secrets"], run_id=old.run_id)
    assert rollback(stale, hermes=env.hermes, store_factory=env.store_factory) is False
    assert (env.pdir() / "user-work.txt").read_text() == "precious"
    assert env.receipt_path().exists()
    assert json.loads(env.receipt_path().read_text())["run_id"] == new.run_id
    assert not any(c[:2] == ["profile", "delete"] for c in env.calls()[-3:])


def test_old_receipt_object_second_rollback_is_inert_after_recreate(env):
    old = run_write(env, plan(env_values={"A_KEY": "1"}))
    rollback(old, hermes=env.hermes, store_factory=env.store_factory)
    assert old.steps_done == [] and old.created_paths == [] and old.env_names_written == []
    new = run_write(env, plan(env_values={"A_KEY": "2"}))
    rollback(old, hermes=env.hermes, store_factory=env.store_factory)
    assert env.pdir().is_dir() and dotenv_read(env.pdir() / ".env")["A_KEY"] == "2"
    assert new.run_id and env.receipt_path().exists()


def test_tampered_receipt_naming_another_profile_does_nothing(env):
    run_write(env, plan(name="victim", env_values={"A_KEY": "1"}))
    (env.pdir("victim") / "work.txt").write_text("keep")
    run_write(env, plan(name="mine"))
    mine = Receipt.load(env.platform, "mine")
    forged = Receipt("victim", [str(env.pdir("victim"))], ["A_KEY"], ["install"], run_id=mine.run_id)
    assert rollback(forged, hermes=env.hermes, store_factory=env.store_factory) is False
    forged2 = Receipt("victim", [str(env.pdir("victim"))], ["A_KEY"], ["install"], run_id="")
    assert rollback(forged2, hermes=env.hermes, store_factory=env.store_factory) is False
    assert (env.pdir("victim") / "work.txt").read_text() == "keep"
    assert dotenv_read(env.pdir("victim") / ".env")["A_KEY"] == "1"
    assert env.receipt_path("victim").exists()


def test_symlinked_marker_is_not_proof(env):
    r = run_write(env, plan())
    marker = env.pdir() / ".trading-agent-run"
    real = env.root / "elsewhere.txt"
    real.write_text(r.run_id + "\n")
    marker.unlink()
    marker.symlink_to(real)
    assert rollback(r, hermes=env.hermes, store_factory=env.store_factory) is False
    assert env.pdir().is_dir()


def test_rollback_returns_true_when_it_acted(env):
    r = run_write(env, plan())
    assert rollback(r, hermes=env.hermes, store_factory=env.store_factory) is True
    assert not env.receipt_path().exists()


# ---------------------------------------------------------------- round 2: cost bound, percent-encoded echoes

def _windows(variants, size=8):
    for v in variants:
        for i in range(0, max(1, len(v) - size + 1)):
            yield v[i:i + size]


@pytest.mark.parametrize("value", [
    "AbCd1234  EfGh5678", "AbCd1234\nEfGh5678", "AbCd1234\tEfGh5678", "AbCd1234\rEfGh5678",
    "AbCd1234\r\nEfGh5678", 'AbCd1234"EfGh5678', "AbCd1234\\EfGh5678", "AbCd1234&=/+%?EfGh5678",
    "Qx9" + "abcdefghij" * 71,
])
@pytest.mark.parametrize("encoder", ["quote", "quote_plus"])
@pytest.mark.parametrize("case", ["upper", "lower"])
def test_report_scrubs_percent_encoded_echo(env, value, encoder, case):
    import re
    from urllib.parse import quote, quote_plus
    enc = (quote(value, safe="") if encoder == "quote" else quote_plus(value))
    if case == "lower":
        enc = re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), enc)
    env.set_control(fail_match="config set model.provider", fail_stderr=f"bad {enc} tail\n")
    with pytest.raises(WriteError) as ei:
        run_write(env, plan(ai=AiChoice("openrouter", ""), env_values={"OPENROUTER_API_KEY": value}))
    rep = ei.value.report
    for w in _windows([value, enc, " ".join(value.split())]):
        assert w not in rep, w
    assert HIDDEN in rep


@pytest.mark.parametrize("shape", ["dots", "token-runs", "no-separators", "userinfo-alphabet"])
def test_report_from_200kb_of_spaceless_stderr_is_fast_and_truncated(env, shape):
    import time
    blob = {"dots": "a." * 100_000, "token-runs": "?" + "token" * 40_000, "no-separators": "Ab1" * 70_000,
            "userinfo-alphabet": "abc.-+:" * 30_000}[shape]
    env.set_control(fail_match="config set auth", fail_stderr=blob, fail_stdout=blob)
    t0 = time.perf_counter()
    with pytest.raises(WriteError) as ei:
        run_write(env, plan(env_values={"A_KEY": "Zq9" + "k" * 40}))
    assert time.perf_counter() - t0 < 5.0
    assert len(ei.value.report) < 1500
    assert not env.pdir().exists()


# ---------------------------------------------------------------- round 3/4: cap context, tail display
# Round 4: the cut is tested on _clean's own output (before the 600 char display hides most of it),
# and end to end with only newlines after the shape so whitespace normalization puts it on display.

# (context before, secret value, plain text after). Each value is long enough that redact_patterns
# hides it when its context is present (hex and base64 runs need 32+ chars), and no fragment of it
# shorter than 32 chars matches any pattern on its own: a fragment cut away from its context leaks.
CUT_SHAPES = {
    "sk-key": ("", "sk-AbCdEfGhIjKlMnOpQrStUvWxYz", ""),
    "hex": ("", "0123456789abcdef0123456789abcdef01234567", ""),
    "b64": ("", "QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Njc4OTBh", ""),
    "bearer": ("Authorization: Bearer ", "ZxCvBnMqWeRtYuIoPaSdFg", ""),
    "userinfo": ("https://", "alice:S3cr3tPassw0rdXYZ", "@host.example/p"),
    "param": ("https://api.example/v1?api_key=", "ZZtop99secretVALue", "&page=2"),
    # longer than CONTEXT and dotted like a JWT (no 32 char base64 run): only ``Bearer`` redacts it, so
    # a tail that runs past the dropped context is caught only by dropping the leading partial token
    "long-bearer": ("Authorization: Bearer ", "eyJhbGciOiJIUzI1NiJ9." + "AbCdEf1234." * 55, ""),
}
CUT_FINAL = "FINAL ERROR LINE"
CUT_VARIANTS = ("spaces", "newlines", "spaceless")
_SPACELESS_RUN = "a." * 50          # no whitespace for 100 chars right before the shape


def _cut_window():
    from trading_agent.profile import writer as w
    return w._RAW_CAP + w._CONTEXT


def _cut_blob(shape, total, offset, variant, after="filler"):
    """`total` chars of text whose shape starts `offset` chars after the start of the redaction window
    (len - CAP - CONTEXT). Before the shape: words separated by spaces or newlines, and for the
    spaceless variant a 100 char run without whitespace. After it: words (`after="filler"`) or only
    newlines (`after="newlines"`, which whitespace normalization collapses), then CUT_FINAL."""
    item = "".join(CUT_SHAPES[shape])
    start = total - _cut_window() + offset
    sep = "\n" if variant == "newlines" else " "
    run = _SPACELESS_RUN if variant == "spaceless" else ""
    n = start - len(run)
    if n < 2:
        return None
    filler = "noise" + sep
    head = (filler * (n // len(filler) + 2))[:n - 1] + sep + run
    rest = total - start - len(item)
    body = rest - len(CUT_FINAL) - 2
    if after == "newlines":
        tail = "\n" * (rest - len(CUT_FINAL) - 1) + CUT_FINAL + "\n"
    else:
        tail = sep + (filler * (body // len(filler) + 2))[:body] + "\n" + CUT_FINAL
    text = head + item + tail
    assert len(text) == total and text.index(item) == start, (shape, total, offset, variant)
    return text


def _cut_offsets(item_len):
    """Every offset where a shape of up to 64 chars straddles the window start (0) or the end of the
    dropped context (CONTEXT, which is also where a plain last-CAP cut would fall), plus a coarse sweep
    of -650..650 in steps of 11 (which is where the long shape gets cut deep inside)."""
    from trading_agent.profile import writer as w
    band = min(item_len, 64) + 8           # a long shape straddles inside the band and in the coarse sweep
    near = set(range(-band, 9)) | set(range(w._CONTEXT - band, w._CONTEXT + 9))
    return sorted(near | set(range(-650, 651, 11)))


def _fragments(value, out):
    grams = {out[i:i + 8] for i in range(len(out) - 7)}
    return [win for win in _windows([value]) if win in grams]


@pytest.mark.parametrize("variant", CUT_VARIANTS)
@pytest.mark.parametrize("total", [17 * 1024, 25 * 1024])
@pytest.mark.parametrize("shape", list(CUT_SHAPES))
def test_clean_keeps_no_fragment_of_a_secret_cut_by_the_window(shape, total, variant):
    """_clean's own output, before the 600 char display, at every offset around both cuts."""
    from trading_agent.profile import writer as w
    _before, value, _after = CUT_SHAPES[shape]
    item_len = len("".join(CUT_SHAPES[shape]))
    leaks, checked, shown = [], 0, 0
    for offset in _cut_offsets(item_len):
        text = _cut_blob(shape, total, offset, variant)
        if text is None:
            continue
        checked += 1
        out = w._clean(text, [])
        found = _fragments(value, out)
        if found:
            leaks.append((offset, found[0]))
        assert out.endswith(CUT_FINAL), (offset, out[-40:])
        # positive control: when whitespace survives between the dropped context and the shape, the
        # whole shape is kept, so it must be there and redacted (the test is not passing by dropping)
        start = total - _cut_window() + offset
        if re.search(r"\s", text[total - _cut_window() + w._CONTEXT:start]):
            assert HIDDEN in out, (offset, out[:80])
            shown += 1
    assert not leaks, f"{len(leaks)} of {checked} offsets leak a fragment, first: {leaks[:3]}"
    assert checked >= 150
    if total == 25 * 1024:
        assert shown > 0


def _e2e_cases():
    from trading_agent.profile import writer as w
    for shape, (before, value, _after) in CUT_SHAPES.items():
        in_value = len(before) + len(value) - 12          # cut leaves the last 12 value chars
        in_context = max(len(before) - 2, 3)               # cut inside the context (or the value start)
        for where, offset in (("window-in-value", -in_value), ("window-in-context", -in_context),
                              ("cap-in-value", w._CONTEXT - in_value),
                              ("cap-in-context", w._CONTEXT - in_context), ("kept", 700)):
            yield pytest.param(shape, offset, where, id=f"{shape}-{where}")


@pytest.mark.parametrize(("shape", "offset", "where"), list(_e2e_cases()))
def test_report_keeps_no_fragment_of_a_secret_cut_by_the_window(env, shape, offset, where):
    """End to end. Only newlines follow the shape, so whitespace normalization pulls whatever survives
    of it into the visible last 600 chars right before the final line: a kept fragment would show."""
    _before, value, _after = CUT_SHAPES[shape]
    blob = _cut_blob(shape, 25 * 1024, offset, "spaces", after="newlines")
    env.set_control(fail_match="config set auth", fail_stderr=blob)
    with pytest.raises(WriteError) as ei:
        run_write(env, plan())
    rep = ei.value.report
    flat = " ".join(rep.split())
    assert not _fragments(value, rep) and not _fragments(value, flat), (where, rep[-200:])
    assert CUT_FINAL in rep
    if where == "kept":
        assert HIDDEN in flat[-120:], rep[-200:]    # the shape is shown, redacted, next to the error


def test_long_stderr_shows_the_error_at_the_end(env):
    blob = "lorem ipsum dolor " * 3000 + "\nFATAL: provider config rejected by hermes\n"
    env.set_control(fail_match="config set auth", fail_stderr=blob)
    with pytest.raises(WriteError) as ei:
        run_write(env, plan())
    assert "FATAL: provider config rejected by hermes" in ei.value.report
    assert len(ei.value.report) < 1500


@pytest.mark.parametrize("shape", ["dots", "token-runs", "no-separators", "userinfo-alphabet"])
def test_spaceless_blob_fails_closed(env, shape):
    blob = {"dots": "a." * 100_000, "token-runs": "?" + "token" * 40_000, "no-separators": "Ab1" * 70_000,
            "userinfo-alphabet": "abc.-+:" * 30_000}[shape]
    env.set_control(fail_match="config set auth", fail_stderr=blob)
    with pytest.raises(WriteError) as ei:
        run_write(env, plan())
    assert "output omitted" in ei.value.report
    assert "Ab1Ab1" not in ei.value.report and "token" not in ei.value.report.replace("config set", "")


# ---------------------------------------------------------------- round 4: the notice survives a trailing newline

NL_SECRET = "sk-NlTrailAbCdEfGhIjKlMnOp"
ONE_LINE_BLOBS = {
    # a 20 KB compact JSON line with a key near its end, and a 200 KB spaceless run with one inside
    "json": '{"items":[' + '{"id":17,"name":"widget"},' * 800 + '{"api_key":"' + NL_SECRET + '"}]}',
    "dots": "a." * 100_000 + NL_SECRET + "." + "a." * 200,
}


@pytest.mark.parametrize("blob", list(ONE_LINE_BLOBS))
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\n\n"])
def test_clean_one_long_line_ending_in_a_newline_is_unstructured(blob, ending):
    from trading_agent.profile import writer as w
    out = w._clean(ONE_LINE_BLOBS[blob] + ending, [])
    assert out == w._UNSTRUCTURED
    assert not any(win in out for win in _windows([NL_SECRET]))


@pytest.mark.parametrize("blob", list(ONE_LINE_BLOBS))
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_report_keeps_the_unstructured_notice_when_output_ends_with_a_newline(env, blob, ending):
    env.set_control(fail_match="config set auth", fail_stderr=ONE_LINE_BLOBS[blob] + ending)
    with pytest.raises(WriteError) as ei:
        run_write(env, plan())
    rep = ei.value.report
    assert "(output omitted: unstructured)" in rep
    for win in _windows([NL_SECRET]):
        assert win not in rep, win


# ---------------------------------------------------------------- URL rules and receipt ownership

@pytest.mark.parametrize("url", [
    "http://example.com/v1", "http://192.168.1.5:9000/v1", "https://api.example.com/v1\u202e/evil",
    "https://api.example.com/v1 x", "https://api.example.com/\u00e9", "https://" + "a" * 600 + ".com/v1",
    "https://user:pw@api.example.com/v1", "ftp://x.test/v1", "https://x.test/v1?k=1", "https://x.test/v1\n"])
def test_custom_base_url_rules_reject_without_echo(url):
    from trading_agent.profile.writer import validate_ai_choice
    with pytest.raises(ValueError) as e:
        validate_ai_choice(AiChoice("custom", url))
    assert url not in str(e.value)
    with pytest.raises(ValueError):
        build_mcp_entry({"kind": "remote-url", "url": url, "auth": {}}, frozenset())


@pytest.mark.parametrize("url", ["https://api.example.com/v1", "http://127.0.0.1:9000/v1",
                                 "http://localhost:8000/v1", "http://[::1]:8000/v1"])
def test_custom_base_url_rules_accept(url):
    from trading_agent.profile.writer import validate_ai_choice
    validate_ai_choice(AiChoice("custom", url))
    validate_ai_choice(AiChoice("ollama", ""))
    validate_ai_choice(AiChoice("lm-studio", ""))


def test_receipt_owns_profile_needs_dir_and_marker(env):
    from trading_agent.profile.writer import receipt_owns_profile
    r = run_write(env, plan(ai=AiChoice("none", "")))
    assert receipt_owns_profile(r, env.hermes)
    stale = Receipt(profile_name="trader", run_id="not-the-marker", steps_done=list(r.steps_done))
    assert not receipt_owns_profile(stale, env.hermes)
    shutil.rmtree(env.pdir())
    assert not receipt_owns_profile(r, env.hermes)
    env.pdir().mkdir(parents=True)          # recreated by someone else: no marker
    assert not receipt_owns_profile(r, env.hermes)
