from __future__ import annotations

import io
import logging
import os
import stat

import pytest
from dotenv.main import DotEnv
from dotenv.variables import parse_variables

from trading_agent import secrets as sec
from trading_agent.secrets import EnvFileStore, choose_store, scrub

SPECIAL = ['a"b', "x#y", "p w", "$HOME", "line1\nline2", "back\\slash", "it's", "tab\tcr\r", " lead and trail ",
           "", "a\\nb", "q=\"#'", "unié☃", "\x07\x08\x0b\x0c", "sk-SECRET123"]


def hermes_read(path):
    """Replicates hermes_cli/env_loader.py: _parse_dotenv_file (DotEnv, interpolate=False, line ~364)
    then _resolve_dotenv_assignments (parse_variables, ${VAR} expansion, line ~392) with an empty env."""
    text = path.read_bytes().decode("utf-8-sig")
    assignments = list(DotEnv(dotenv_path=None, stream=io.StringIO(text), interpolate=False).parse())
    resolved: dict = {}
    for name, value in assignments:
        if value is not None:
            value = "".join(atom.resolve(dict(resolved)) for atom in parse_variables(value))
        resolved[name] = value
    return resolved


def mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_env_file_mode_is_0600(tmp_path):
    p = tmp_path / ".env"
    EnvFileStore(p).set("API_KEY", "sk-SECRET123")
    assert mode(p) == 0o600


def test_looser_existing_mode_ends_0600(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\n")
    p.chmod(0o666)
    EnvFileStore(p).set("B", "2")
    assert mode(p) == 0o600


@pytest.mark.parametrize("value", SPECIAL)
def test_roundtrip_special_values(tmp_path, value):
    p = tmp_path / ".env"
    store = EnvFileStore(p)
    store.set("BEFORE", "x")
    store.set("KEY_ONE", value)
    store.set("AFTER", "y")
    got = hermes_read(p)
    assert got["KEY_ONE"] == value
    assert got["BEFORE"] == "x" and got["AFTER"] == "y"
    assert p.read_text().count("\n") == 3  # multi-line value stays on one physical line


def test_roundtrip_update_in_place_with_special(tmp_path):
    p = tmp_path / ".env"
    s = EnvFileStore(p)
    s.set("K", "one")
    s.set("K", 'two "2" #\nx')
    assert hermes_read(p)["K"] == 'two "2" #\nx'


def test_interpolation_value_rejected_without_leak(tmp_path):
    p = tmp_path / ".env"
    with pytest.raises(ValueError) as e:
        EnvFileStore(p).set("K", "sk-SECRET123${HOME}")
    assert "K" in str(e.value) and "SECRET123" not in str(e.value)
    assert not p.exists()


def test_other_lines_comments_order_preserved_update_and_append(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# Hermes header\n\nA=1\n# note\nexport B = old\nC=3")  # no trailing newline
    s = EnvFileStore(p)
    s.set("B", "new")
    assert p.read_text() == "# Hermes header\n\nA=1\n# note\nB=new\nC=3"  # untouched, still no newline
    s.set("D", "4")
    assert p.read_text() == "# Hermes header\n\nA=1\n# note\nB=new\nC=3\nD=4\n"


def test_duplicate_name_collapsed_to_one_line(tmp_path):
    p = tmp_path / ".env"
    p.write_text("K=old1\nZ=z\nK=old2\n")
    EnvFileStore(p).set("K", "new")
    assert p.read_text() == "K=new\nZ=z\n"


def test_creates_file_and_no_leftovers(tmp_path):
    d = tmp_path / "profile"
    p = d / ".env"
    EnvFileStore(p).set("K", "sk-SECRET123")
    assert os.listdir(d) == [".env"]


def test_remove_deletes_only_that_name(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# c\nA=1\nKEY=2\nKEY_2=3\nexport KEY=4\n")
    s = EnvFileStore(p)
    s.remove("KEY")
    assert p.read_text() == "# c\nA=1\nKEY_2=3\n"
    s.remove("MISSING")
    assert p.read_text() == "# c\nA=1\nKEY_2=3\n"
    EnvFileStore(tmp_path / "none.env").remove("A")
    assert not (tmp_path / "none.env").exists()


def test_exists_is_bool_and_holds_no_value(tmp_path):
    p = tmp_path / ".env"
    s = EnvFileStore(p)
    assert s.exists("K") is False
    s.set("K", "sk-SECRET123")
    assert s.exists("K") is True
    assert s.exists("OTHER") is False
    assert "SECRET123" not in repr(vars(s))


def test_failed_replace_leaves_original_and_no_tmp(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("A=1\n")
    before = p.read_bytes()

    def boom(*a, **k):
        raise OSError("disk says no")

    monkeypatch.setattr(sec.os, "replace", boom)
    with pytest.raises(OSError):
        EnvFileStore(p).set("B", "sk-SECRET123")
    assert p.read_bytes() == before
    assert os.listdir(tmp_path) == [".env"]


@pytest.mark.parametrize("bad", ["lower", "1A", "A-B", "A B", "", "A\n", "A=B", "export A"])
def test_invalid_name_rejected(tmp_path, bad):
    s = EnvFileStore(tmp_path / ".env")
    for fn in (lambda: s.set(bad, "sk-SECRET123"), lambda: s.exists(bad), lambda: s.remove(bad)):
        with pytest.raises(ValueError) as e:
            fn()
        assert "SECRET123" not in str(e.value)
    assert not (tmp_path / ".env").exists()


def test_nul_value_rejected_without_leak(tmp_path):
    with pytest.raises(ValueError) as e:
        EnvFileStore(tmp_path / ".env").set("K", "sk-SECRET123\x00")
    assert "K" in str(e.value) and "SECRET123" not in str(e.value) and "SECRET123" not in repr(e.value)
    assert not (tmp_path / ".env").exists()


def test_lone_surrogate_rejected_without_leak(tmp_path):
    with pytest.raises(ValueError) as e:
        EnvFileStore(tmp_path / ".env").set("K", "sk-SECRET123\ud800")
    assert "SECRET123" not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


def test_never_logs_value(tmp_path, caplog, capsys):
    with caplog.at_level(logging.DEBUG):
        s = EnvFileStore(tmp_path / ".env")
        s.set("K", "sk-SECRET123")
        s.exists("K")
        s.set("K", "sk-SECRET456")
        s.remove("K")
    assert all("SECRET" not in r.getMessage() for r in caplog.records)
    out = capsys.readouterr()
    assert "SECRET" not in out.out + out.err


def test_repr_and_str_show_path_only(tmp_path):
    s = EnvFileStore(tmp_path / ".env")
    s.set("K", "sk-SECRET123")
    for text in (repr(s), str(s)):
        assert "SECRET123" not in text
        assert str(tmp_path / ".env") in text


def test_choose_store_is_env_file_in_profile(tmp_path):
    s = choose_store(object(), tmp_path / "prof")
    assert isinstance(s, EnvFileStore)
    s.set("K", "v")
    assert (tmp_path / "prof" / ".env").exists()


def test_scrub_removes_value_and_quoted_form(tmp_path):
    p = tmp_path / ".env"
    value = 'sk-SEC"RET #1\nz'
    EnvFileStore(p).set("K", value)
    line = p.read_text()
    out = scrub(f"hermes failed: {value!r} / raw {value} / env {line}", [value])
    assert "SEC" not in out.replace("[hidden]", "") and "RET" not in out
    assert "[hidden]" in out
    assert scrub(f"x {line.split('=', 1)[1].strip()} y", [value]) == "x [hidden] y"


def test_scrub_longest_first_and_substrings():
    assert scrub("key sk-abc-longer end", ["sk-abc", "sk-abc-longer"]) == "key [hidden] end"
    assert scrub("aXbXXc", ["X", "XX"]) == "a[hidden]b[hidden]c"


def test_scrub_regex_metacharacters():
    assert scrub("tok .*+?(a) and [x] ok", [".*+?(a)", "[x]"]) == "tok [hidden] and [hidden] ok"
    assert scrub("abc", [".*"]) == "abc"
    assert scrub("a.c abc", ["a.c"]) == "[hidden] abc"


def test_scrub_ignores_empty_secrets():
    assert scrub("hello", ["", "hello"]) == "[hidden]"
    assert scrub("hello", ["", ""]) == "hello"
    assert scrub("hello", []) == "hello"


def test_scrub_does_not_rescan_replacement():
    assert scrub("sk-1 hid", ["hid", "sk-1"]) == "[hidden] [hidden]"
