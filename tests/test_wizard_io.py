from __future__ import annotations

import builtins
import getpass
import io as _io
import sys as _sys

import pytest as _pytest

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
    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True})())
    io = ConsoleIO()
    assert io.confirm("ok?") is True
    assert io.confirm("ok?") is False
    assert io.secret("key") == "s3cret"
    assert io.ask("name") == "hello"
    io.say("plain text")
    assert "plain text" in capsys.readouterr().out


# ---- ConsoleIO behavior the CLI relies on (secret never echoes, invalid input never printed, EOF raises)


class _PipeStdin(_io.StringIO):
    def isatty(self):
        return False


def test_console_secret_from_a_pipe_reads_one_line_and_prints_neither_it_nor_anything_derived(monkeypatch, capsys):
    monkeypatch.setattr(_sys, "stdin", _PipeStdin("hunter2-SECRET\nnext\n"))
    got = ConsoleIO().secret("Paste it:")
    assert got == "hunter2-SECRET"
    out = capsys.readouterr()
    assert "hunter2" not in out.out + out.err and "Paste it:" in out.out


def test_console_secret_on_a_terminal_uses_getpass(monkeypatch):
    class Tty(_PipeStdin):
        def isatty(self):
            return True
    monkeypatch.setattr(_sys, "stdin", Tty(""))
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "typed")
    assert ConsoleIO().secret("Paste it:") == "typed"


def test_console_secret_at_end_of_input_raises_eof(monkeypatch):
    monkeypatch.setattr(_sys, "stdin", _PipeStdin(""))
    with _pytest.raises(EOFError):
        ConsoleIO().secret("Paste it:")


def test_console_choose_reprompts_without_printing_the_bad_input(monkeypatch, capsys):
    monkeypatch.setattr(_sys, "stdin", _PipeStdin("zz-JUNK-zz\n9\n0\n-1\nb-JUNK\n2\n"))
    assert ConsoleIO().choose("Pick", [Option("a", "A"), Option("b", "B")]) == "b"
    out = capsys.readouterr().out
    assert "JUNK" not in out and out.count("Type a number from the list.") == 5


def test_console_confirm_reprompts_without_printing_the_bad_input(monkeypatch, capsys):
    monkeypatch.setattr(_sys, "stdin", _PipeStdin("zz-JUNK-zz\nmaybe-JUNK\ny\n"))
    assert ConsoleIO().confirm("Sure?") is True
    out = capsys.readouterr().out
    assert "JUNK" not in out and out.count("Type y or n.") == 2


def test_console_confirm_default_applies_only_when_given(monkeypatch, capsys):
    monkeypatch.setattr(_sys, "stdin", _PipeStdin("\n\ny\n"))
    io = ConsoleIO()
    assert io.confirm("Remove?", default=False) is False
    assert io.confirm("Remove?", default=True) is True
    assert io.confirm("Remove?") is True          # no default: an empty line re-asks, then y
    assert "[y/N]" in capsys.readouterr().out


def test_console_input_at_end_of_input_raises_eof(monkeypatch):
    monkeypatch.setattr(_sys, "stdin", _PipeStdin(""))
    for call in (lambda io: io.ask("Name?"), lambda io: io.confirm("Sure?"),
                 lambda io: io.choose("Pick", [Option("a", "A")])):
        with _pytest.raises(EOFError):
            call(ConsoleIO())
