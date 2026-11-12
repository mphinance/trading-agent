from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from trading_agent.platform import RunResult, shell_has_var, shell_vars


class FakePlatform:
    def __init__(self, home, environ=None, which_map=None, run_results=None, config_home=None, files=None, dirs=None):
        self._home = Path(home)
        self._config_home = Path(config_home) if config_home else self._home / ".config"
        self._environ = dict(environ or {})
        self._which = dict(which_map or {})
        self._results = list(run_results or [])
        self.run_calls: list[list[str]] = []
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
        if self._results:
            return self._results.pop(0)
        return RunResult(0, "", "", False)
