from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from trading_agent.platform import RunResult, shell_has_var


class FakePlatform:
    def __init__(self, home, environ=None, which_map=None, run_results=None, config_home=None):
        self._home = Path(home)
        self._config_home = Path(config_home) if config_home else self._home / ".config"
        self._environ = dict(environ or {})
        self._which = dict(which_map or {})
        self._results = list(run_results or [])
        self.run_calls: list[list[str]] = []

    def home(self) -> Path:
        return self._home

    def config_home(self) -> Path:
        return self._config_home

    def environ(self) -> Mapping[str, str]:
        return self._environ

    def which(self, name: str) -> Path | None:
        p = self._which.get(name)
        return Path(p) if p else None

    def login_shell_has_var(self, name: str) -> bool:
        return shell_has_var(self, name)

    def run(self, argv: Sequence[str], *, timeout: float,
            env: Mapping[str, str] | None = None, stdin: str | None = None) -> RunResult:
        self.run_calls.append(list(argv))
        if self._results:
            return self._results.pop(0)
        return RunResult(0, "", "", False)
