from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from trading_agent.findings import Finding
from trading_agent.platform import RunResult, shell_has_var, shell_vars


class FakePlatform:
    def __init__(self, home, environ=None, which_map=None, run_results=None, config_home=None, files=None, dirs=None):
        self._home = Path(home)
        self._config_home = Path(config_home) if config_home else self._home / ".config"
        self._environ = dict(environ or {})
        self._which = dict(which_map or {})
        self._results = list(run_results or [])
        self.run_calls: list[list[str]] = []
        self.run_envs: list = []
        self.run_timeouts: list[float] = []
        self.files: dict[Path, str] = {Path(k): v for k, v in (files or {}).items()}
        self.dirs: dict[Path, list[str]] = {Path(k): list(v) for k, v in (dirs or {}).items()}
        self.calls: list[str] = []

    def home(self) -> Path:
        self.calls.append("home")
        return self._home

    def config_home(self) -> Path:
        self.calls.append("config_home")
        return self._config_home

    def environ(self) -> Mapping[str, str]:
        self.calls.append("environ")
        return self._environ

    def which(self, name: str) -> Path | None:
        self.calls.append("which")
        p = self._which.get(name)
        return Path(p) if p else None

    def read_text(self, path: Path) -> str:
        self.calls.append("read_text")
        try:
            return self.files[Path(path)]
        except KeyError:
            raise FileNotFoundError(str(path)) from None

    def path_exists(self, path: Path) -> bool:
        self.calls.append("path_exists")
        return Path(path) in self.files

    def list_dir(self, path: Path) -> list[str]:
        self.calls.append("list_dir")
        try:
            return list(self.dirs[Path(path)])
        except KeyError:
            raise FileNotFoundError(str(path)) from None

    def login_shell_has_var(self, name: str) -> bool:
        self.calls.append("login_shell_has_var")
        return shell_has_var(self, name)

    def login_shell_vars(self, names: Sequence[str]) -> frozenset[str]:
        self.calls.append("login_shell_vars")
        return shell_vars(self, names)

    def run(self, argv: Sequence[str], *, timeout: float,
            env: Mapping[str, str] | None = None, stdin: str | None = None) -> RunResult:
        self.calls.append("run")
        self.run_calls.append(list(argv))
        self.run_envs.append(None if env is None else dict(env))
        self.run_timeouts.append(timeout)
        if self._results:
            return self._results.pop(0)
        return RunResult(0, "", "", False)


# ---------------------------------------------------------------- wizard fakes

class ScriptExhausted(Exception):
    """The wizard asked for more answers than the script has (also how tests simulate an interrupt)."""


class ScriptedIO:
    """Feeds a list of answers (choose: option id, confirm: bool, secret/ask: str) and records everything.

    ``prompts`` holds (kind, prompt, [(option id, label), ...]); ``said`` holds every ``say`` text.
    ``everything()`` is all text a user could have seen, for leak assertions.
    """

    def __init__(self, answers, on_prompt=None):
        self.answers = list(answers)
        self.said: list[str] = []
        self.prompts: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.details: list[str] = []
        self.on_prompt = on_prompt
        self.confirm_defaults: list = []

    def say(self, text: str) -> None:
        self.said.append(text)

    def _next(self, kind, prompt, options=()):
        self.prompts.append((kind, prompt, [(o.id, o.label) for o in options]))
        self.details.extend(o.detail for o in options if o.detail)
        if self.on_prompt:
            self.on_prompt(self)
        if not self.answers:
            raise ScriptExhausted(f"no answer left for {kind}: {prompt[:60]!r}")
        return self.answers.pop(0)

    def choose(self, prompt, options):
        ids = [o.id for o in options]
        a = self._next("choose", prompt, options)
        assert a in ids, f"scripted answer {a!r} not offered; offered {ids}; prompt {prompt[:80]!r}"
        return a

    def confirm(self, prompt, default=None):
        self.confirm_defaults.append(default)
        a = self._next("confirm", prompt)
        assert isinstance(a, bool), f"confirm needs a bool, got {a!r}; prompt {prompt[:80]!r}"
        return a

    def secret(self, prompt):
        a = self._next("secret", prompt)
        assert isinstance(a, str), f"secret needs a str, got {a!r}; prompt {prompt[:80]!r}"
        return a

    def ask(self, prompt):
        a = self._next("ask", prompt)
        assert isinstance(a, str), f"ask needs a str, got {a!r}; prompt {prompt[:80]!r}"
        return a

    # helpers for assertions
    def kinds(self):
        return [k for k, _, _ in self.prompts]

    def option_ids(self, index=-1):
        return [i for i, _ in self.prompts[index][2]]

    def prompt_texts(self):
        out = []
        for _, prompt, opts in self.prompts:
            out.append(prompt)
            out.extend(label for _, label in opts)
        return out

    def everything(self) -> str:
        return "\n".join(self.said + self.prompt_texts() + self.details)


class FakeHermes:
    """Just the HermesCli surface the wizard touches. write_profile is monkeypatched in unit tests."""

    def __init__(self, platform, root, version="Hermes Agent v0.23.1 (2026.9.24)", raises=None, existing=()):
        self.platform = platform
        self.root = Path(root)
        self._version = version
        self.raises = raises
        self.existing = set(existing)
        self.version_calls = 0
        self.exists_calls: list[str] = []

    def version(self):
        self.version_calls += 1
        if self.raises:
            raise self.raises
        return self._version

    def profile_exists(self, name):
        self.exists_calls.append(name)
        return name in self.existing

    def profile_dir(self, name):
        return self.root / "profiles" / name


class FakeStore:
    def __init__(self, existing=()):
        self.names = set(existing)
        self.set_calls: list[str] = []

    def set(self, name, value):
        self.set_calls.append(name)
        self.names.add(name)

    def exists(self, name):
        return name in self.names

    def remove(self, name):
        self.names.discard(name)


def finding(fid, kind, what, names, *, state="present", where="environment variable",
            reusable="copy-with-consent"):
    return Finding(id=fid, kind=kind, what=what, where=where, state=state, names=tuple(names), reusable=reusable)


def env_finding(name, kind="ai-key", *, where="environment variable", reusable="copy-with-consent",
                state="present", what=None):
    return finding(f"env:{name}", kind, what or f"{name} credential", [name], where=where,
                   reusable=reusable, state=state)


def port_finding(name, *, present=True):
    return finding(f"ai-local:{name}", "ai-local", f"Something is listening on the usual {name} port",
                   [name], state="present" if present else "absent", where="127.0.0.1:1", reusable="none")


def cli_finding(name, *, present=True):
    return finding(f"cli:{name}", "ai-cli", f"{name} CLI (found, not tested)", [name],
                   state="present" if present else "absent", where=f"/usr/bin/{name}", reusable="reference-only")


# Fixed, random-looking 24 character secrets with no shared prefix. Every 4 character window of them is
# checked, so a masked or partial leak (v[:6], v[-7:], "ends in ...") is caught.
K1 = "aQ7xM2kZp9Wv4TnR8yJd3HsB"
K2 = "Lr5Cg8bN2uXe6FtY1oVm9PzD"
K3 = "Wm3Hk7sQ4dJf9ZxA2nUv8GcR"
K4 = "Yt6Bp1eN5qLc8VhS3zKw7MaJ"
K5 = "Dn4Rz9vKc2Ws7TqH5jXb8MeG"
ALL_SECRETS = (K1, K2, K3, K4, K5)


def windows(secret: str, size: int = 4):
    return [secret[i:i + size] for i in range(len(secret) - size + 1)]


def assert_no_leak(text: str, *secrets: str) -> None:
    """No whole secret and no 4 character window of any secret may appear in TEXT."""
    for secret in secrets:
        for w in windows(secret):
            assert w not in text, f"a piece of a secret leaked: {w[:2]}..."


def sweep_tree(root, secrets, *, allow=()) -> None:
    """Every file under ROOT (state file, receipt, hermes call log, config.yaml) except ALLOW must be clean."""
    allowed = {Path(a) for a in allow}
    for p in Path(root).rglob("*"):
        if p.is_file() and p not in allowed:
            assert_no_leak(p.read_bytes().decode("utf-8", errors="replace"), *secrets)


import json as _json  # noqa: E402
import re as _re  # noqa: E402

_CONSUMER_KEY = r"(?:ai|broker:[a-z0-9_-]+/[a-z0-9_-]+|lib:[a-z0-9-]+):[A-Z_][A-Z0-9_]*"
_STATE_KEYS = {
    "profile_name": r"[a-z][a-z0-9_-]{0,31}", "scan.apps": r"yes|no", "ai.kind": r"[a-z-]+",
    "ai.ref": r"https?://[A-Za-z0-9.:/_\[\]-]+|[A-Za-z0-9._+-]*", "ai.key_env": r"[A-Z_][A-Z0-9_]*|",
    "ai.private": r"[01]", "ai.verify": r"[a-z-]+", "broker.slug": r"[a-z0-9_-]+", "broker.route": r"[a-z0-9_-]*",
    "broker.fp": r"[0-9a-f]{16}", "broker.verify": r"[a-z]+", "skills": r"[a-z0-9,-]*", "skills.done": r"1",
}


def assert_state_is_clean(text: str) -> None:
    """The saved state holds only known decision keys, with values of a non-secret shape (so a hash, a prefix
    or any other trace of a secret under any key, known or new, is caught)."""
    if not text:
        return
    doc = _json.loads(text)
    assert set(doc) == {"current", "decisions"}
    for key, value in doc["decisions"].items():
        if key in _STATE_KEYS:
            assert _re.fullmatch(_STATE_KEYS[key], value), f"state value for {key} has an unexpected shape"
        elif key.startswith("declined:"):
            assert value == "1" and _re.fullmatch(r"declined:[A-Za-z0-9_.:-]+", key)
        else:
            assert _re.fullmatch(_CONSUMER_KEY, key) and value in ("skip", "keep-existing"), f"unknown state key {key[:20]}"
