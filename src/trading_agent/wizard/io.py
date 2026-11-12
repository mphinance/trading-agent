"""The wizard's only door to a human. Tests inject a scripted IO; ConsoleIO is the plain terminal one.

``secret`` must never echo. ``ask`` is plain text input for things that are not secret (a profile
name, an address, a search word, a mode switch). Typing ``<`` on an ``ask`` prompt means Back.
"""
from __future__ import annotations

import getpass
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
    def confirm(self, prompt: str) -> bool: ...
    def secret(self, prompt: str) -> str: ...
    def ask(self, prompt: str) -> str: ...


class ConsoleIO:
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

    def confirm(self, prompt: str) -> bool:
        while True:
            raw = input(f"{prompt} [y/n] ").strip().lower()
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            print("Type y or n.")

    def secret(self, prompt: str) -> str:
        return getpass.getpass(f"{prompt} ")

    def ask(self, prompt: str) -> str:
        return input(f"{prompt} ")
