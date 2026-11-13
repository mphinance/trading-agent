"""The wizard's only door to a human. Tests inject a scripted IO; ConsoleIO is the plain terminal one.

``secret`` must never echo. ``ask`` is plain text input for things that are not secret (a profile
name, an address, a search word, a mode switch). Typing ``<`` on an ``ask`` prompt means Back.
"""
from __future__ import annotations

import getpass
import sys
from dataclasses import dataclass
from typing import Protocol, Sequence

BACK_INPUT = "<"


@dataclass(frozen=True)
class Option:
    id: str
    label: str
    detail: str = ""


class IO(Protocol):
    def say(self, text: str) -> None: ...
    def choose(self, prompt: str, options: Sequence[Option]) -> str: ...
    def confirm(self, prompt: str, default: bool | None = None) -> bool: ...
    def secret(self, prompt: str) -> str: ...
    def ask(self, prompt: str) -> str: ...


class ConsoleIO:
    """Plain terminal IO. Ctrl-C and end of input raise KeyboardInterrupt and EOFError (the CLI handles both).
    Nothing the user types is ever printed back, and ``secret`` never echoes."""

    def say(self, text: str) -> None:
        print(text)

    def choose(self, prompt: str, options: Sequence[Option]) -> str:
        ids = [o.id for o in options]
        print(prompt)
        for n, o in enumerate(options, 1):
            print(f"  {n}. {o.label}")
            if o.detail:
                print(f"     {o.detail}")
        while True:
            raw = input("> ").strip()
            if raw in ids:
                return raw
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return ids[int(raw) - 1]
            print("Type a number from the list.")

    def confirm(self, prompt: str, default: bool | None = None) -> bool:
        """With ``default`` set, an empty answer means that default; without it, only y or n is accepted."""
        hint = "[y/n]" if default is None else ("[Y/n]" if default else "[y/N]")
        while True:
            raw = input(f"{prompt} {hint} ").strip().lower()
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            if raw == "" and default is not None:
                return default
            print("Type y or n.")

    def secret(self, prompt: str) -> str:
        if sys.stdin.isatty():
            return getpass.getpass(f"{prompt} ")      # no echo on a terminal
        # Piped input: read one line, print neither it nor anything derived from it.
        sys.stdout.write(f"{prompt} ")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\r\n")

    def ask(self, prompt: str) -> str:
        return input(f"{prompt} ")
