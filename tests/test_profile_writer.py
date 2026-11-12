from __future__ import annotations

import io
import json
import os
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

    def receipt_path(self) -> Path:
        return self.config_home / "trading-agent" / "receipt.json"

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
    fp = FakePlatform(tmp_path, environ={"PATH": "/usr/bin"})
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


def test_remote_header_without_a_source_is_omitted():
    entry = build_mcp_entry(REMOTE_ROUTE, {"TRADIER_API_KEY"})
    assert entry["headers"] == {"API_KEY": "${TRADIER_API_KEY}"}


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
    assert Receipt.load(env.platform).profile_name == "trader"


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
    changed = {k for k in set(pre) | set(post) if pre.get(k, "ABSENT") != post.get(k, "ABSENT")}
    assert changed
    for k in changed:
        assert k == pdir or k.startswith(pdir + os.sep) or k == rpath or k == receipt_dir, k
    assert (env.hermes_home / "config.yaml").read_text() == "global: 1\n"
    assert (other / "f").read_text() == "x"


# ---------------------------------------------------------------- providers

def calls_config_set(env):
    return [c for c in env.calls() if c[:1] == ["-p"] and c[2:4] == ["config", "set"]]


@pytest.mark.parametrize("ai,expected", [
    (AiChoice("none", ""), {}),
    (AiChoice("openrouter", ""), {"provider": "openrouter"}),
    (AiChoice("custom", "https://api.venice.ai/api/v1", "VENICE_API_KEY"),
     {"provider": "custom", "base_url": "https://api.venice.ai/api/v1", "key_env": "VENICE_API_KEY"}),
    (AiChoice("custom", "https://api.openai.com/v1", ""),
     {"provider": "custom", "base_url": "https://api.openai.com/v1"}),
    (AiChoice("ollama", ""), {"provider": "custom", "base_url": "http://127.0.0.1:11434/v1"}),
    (AiChoice("lm-studio", ""), {"provider": "custom", "base_url": "http://127.0.0.1:1234/v1"}),
])
def test_provider_config_keys(env, ai, expected):
    run_write(env, plan(ai=ai))
    cfg = read_cfg(env)
    assert cfg["model"] == expected or (expected == {} and cfg.get("model") in ({}, None))
    keys = [c[4] for c in calls_config_set(env)]
    assert keys[0] == "auth.adopt_external_logins"
    assert [k for k in keys[1:]] == [f"model.{k}" for k in expected]
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


def test_rollback_of_listed_files_skips_symlinks_and_outside(env, tmp_path):
    r = run_write(env, plan())
    outside = env.root / "o.txt"
    outside.write_text("o")
    (env.pdir() / "l").symlink_to(outside)
    partial = Receipt(profile_name="trader", created_paths=[str(env.pdir() / "l"), str(outside),
                                                            str(env.pdir() / "SOUL.md")],
                      env_names_written=[], steps_done=["install"])
    rollback(partial, hermes=env.hermes, store_factory=env.store_factory)
    assert outside.read_text() == "o"
    assert (env.pdir() / "l").is_symlink()          # symlink entries are never followed or removed
    assert not (env.pdir() / "SOUL.md").exists()    # a plain file inside the profile dir was removed
    assert env.pdir().is_dir()                      # profile dir itself not listed: left alone


def test_rollback_removes_only_written_env_names(env):
    r = run_write(env, plan(env_values={"A_KEY": "1", "B_KEY": "2"}))
    (env.pdir() / ".env").write_text((env.pdir() / ".env").read_text() + "FOREIGN=keep\n")
    partial = Receipt("trader", [], ["A_KEY"], ["install", "secrets"])
    rollback(partial, hermes=env.hermes, store_factory=env.store_factory)
    got = dotenv_read(env.pdir() / ".env")
    assert "A_KEY" not in got and got["B_KEY"] == "2" and got["FOREIGN"] == "keep"


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


def test_rollback_keeps_other_runs_receipt(env):
    run_write(env, plan(name="alpha"))
    stale = Receipt("beta", [], [], [])
    rollback(stale, hermes=env.hermes, store_factory=env.store_factory)
    assert json.loads(env.receipt_path().read_text())["profile_name"] == "alpha"


def test_receipt_roundtrip_and_validation(env):
    r = Receipt("trader", ["/a"], ["K"], ["install"], ai_choice={"kind": "none", "ref": ""},
                broker={"slug": "x", "route_id": "y"}, skills=["s"])
    r.save(env.platform)
    assert Receipt.load(env.platform) == r
    env.receipt_path().write_text("{not json")
    with pytest.raises(receipt_mod.ReceiptError):
        Receipt.load(env.platform)
    env.receipt_path().write_text(json.dumps({"profile_name": 1}))
    with pytest.raises(receipt_mod.ReceiptError):
        Receipt.load(env.platform)
    env.receipt_path().unlink()
    assert Receipt.load(env.platform) is None


def test_receipt_save_is_atomic_no_tmp_left(env):
    Receipt("trader", [], [], []).save(env.platform)
    leftovers = [p.name for p in env.receipt_path().parent.iterdir()]
    assert leftovers == ["receipt.json"]
