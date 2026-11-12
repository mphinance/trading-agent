from __future__ import annotations

import builtins
import getpass

from trading_agent.wizard.io import ConsoleIO, Option


def test_console_io_choose_by_number_and_id(monkeypatch, capsys):
    answers = iter(["9", "2", "b"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    io = ConsoleIO()
    opts = [Option("a", "Alpha"), Option("b", "Beta", "a detail line")]
    assert io.choose("Pick", opts) == "b"       # "9" is out of range and is asked again, "2" is Beta
    assert io.choose("Pick", opts) == "b"       # an option id also works
    out = capsys.readouterr().out
    assert "Alpha" in out and "a detail line" in out


def test_console_io_confirm_secret_ask_say(monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    answers = iter(["maybe", "y", "n", "hello"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "s3cret")
    io = ConsoleIO()
    assert io.confirm("ok?") is True
    assert io.confirm("ok?") is False
    assert io.secret("key") == "s3cret"
    assert io.ask("name") == "hello"
    io.say("plain text")
    assert "plain text" in capsys.readouterr().out
