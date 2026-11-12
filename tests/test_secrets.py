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


# ---- redact_patterns (pattern based redaction for reports) ----

@pytest.mark.parametrize("text,gone", [
    ("key sk-abcdef1234567890XYZ here", "sk-abcdef1234567890XYZ"),
    ("key sk-or-v1-0123456789abcdef0123 here", "0123456789abcdef0123"),
    ("sk-ant-api03-AbCdEf_GhIj-KlMnOp", "AbCdEf_GhIj-KlMnOp"),
    ("Authorization: Bearer abc.def-ghi_jkl123456", "abc.def-ghi_jkl123456"),
    ("bearer ZZZZZZZZZZZZ", "ZZZZZZZZZZZZ"),
    ("hash 0123456789abcdef0123456789abcdef0123 end", "0123456789abcdef0123456789abcdef0123"),
    ("b64 QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Njc4OTA end", "QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Njc4OTA"),
    ("GET https://user:hunter2pw@host.example/path", "hunter2pw"),
    ("GET https://tokenonly123@host.example/path", "tokenonly123"),
    ("GET https://h.example/p?api_key=abc123&x=1", "abc123"),
    ("GET https://h.example/p?x=1&access_token=zzz999", "zzz999"),
    ("GET https://h.example/p?sig=Qm9ndXNWYWx1ZTEyMzQ1", "Qm9ndXNWYWx1ZTEyMzQ1"),
    ("GET https://h.example/p?code=A1b2C3d4E5f6G7h8I9j0", "A1b2C3d4E5f6G7h8I9j0"),
])
def test_redact_patterns_removes(text, gone):
    out = sec.redact_patterns(text)
    assert gone not in out
    assert sec.HIDDEN in out


@pytest.mark.parametrize("text", [
    "profile 'trader' already exists at /home/someone/.hermes/profiles/trader",
    "Error: Cannot install a distribution as 'default'",
    "hermes exited 1 on step config",
    "see https://example.com/docs/page?lang=en&page=2",
    "task sk-learn is fine",
    "ALPACA_API_KEY",
    "",
])
def test_redact_patterns_leaves_plain_text(text):
    assert sec.redact_patterns(text) == text


def test_redact_patterns_keeps_url_host_and_path():
    out = sec.redact_patterns("https://user:pw12345@host.example/a/b?token=abcdefgh1")
    assert out.startswith("https://") and "host.example/a/b" in out


def test_redact_patterns_idempotent():
    once = sec.redact_patterns("Bearer abcdefghijklmnop sk-abcdef1234567890")
    assert sec.redact_patterns(once) == once


def test_check_secret_names_only():
    sec.check_secret("A_KEY", 'a"b #$ x\n')
    with pytest.raises(ValueError) as e:
        sec.check_secret("a_key", "v")
    with pytest.raises(ValueError) as e:
        sec.check_secret("A_KEY", "pre${HOME}post")
    assert "pre" not in str(e.value) and "post" not in str(e.value)


# ---- cost bounds and percent-encoded forms ----

import time
from urllib.parse import quote, quote_plus

_N = 100_000
PATHOLOGICAL = {
    "dot-runs": "a." * (_N // 2), "colon-runs": "a:" * (_N // 2), "slashes": "//" * (_N // 2),
    "ats": "@" * _N, "percents": "%" * _N, "equals": "=" * _N, "q-equals": "?=" * (_N // 2),
    "q-a-equals": "?a=" * (_N // 3), "one-token": "A1" * (_N // 2), "b64-ish": "aB3+" * (_N // 4),
    "userinfo-alphabet": ("abc.-+:" * _N)[:_N], "schemes": "http://" * (_N // 7), "a-slash-slash": "a://" * (_N // 4),
    "q-key-runs": "?key" * (_N // 4), "q-token-runs": "?" + "token" * (_N // 5), "q-auth-runs": "?" + "auth" * (_N // 4),
    "amp-pairs": "&a=1" * (_N // 4), "long-hex": "f" * _N, "sk-runs": "sk-" * (_N // 3),
    "bearer-runs": "Bearer " * (_N // 7), "bearer-long": "Bearer " + "a" * _N, "long-param": "?x=" + "a" * _N,
    "scheme-no-slash": "a" * _N + "://", "userinfo-no-at": "a://" + "b" * _N,
}


@pytest.mark.parametrize("name", list(PATHOLOGICAL))
def test_redact_patterns_is_fast_on_pathological_input(name):
    text = PATHOLOGICAL[name]
    t0 = time.perf_counter()
    sec.redact_patterns(text)
    assert time.perf_counter() - t0 < 1.0, name


NASTY = ["AbCd1234  EfGh5678", "AbCd1234\nEfGh5678", "AbCd1234\tEfGh5678", "AbCd1234\rEfGh5678",
         "AbCd1234\r\nEfGh5678", 'AbCd1234"EfGh5678', "AbCd1234\\EfGh5678", "AbCd1234&=/+%?EfGh5678",
         "Qx9" + "abcdefghij" * 71]


@pytest.mark.parametrize("value", NASTY)
def test_scrub_percent_encoded_forms(value):
    forms = {quote(value, safe=""), quote_plus(value)}
    forms |= {__import__("re").sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), f) for f in forms}
    for f in forms:
        out = scrub(f"hermes exited 1: {f} tail", [value])
        assert out == f"hermes exited 1: {sec.HIDDEN} tail", f[:40]


def test_scrub_plain_value_forms_unchanged():
    assert scrub("x sk-ABC123 y", ["sk-ABC123"]) == f"x {sec.HIDDEN} y"
    assert scrub("nothing here", ["zz9"]) == "nothing here"


def test_scrub_is_linear_with_many_secrets():
    text = "a b " * 250_000
    t0 = time.perf_counter()
    scrub(text, [f"secret value {i} &=%" for i in range(200)])
    assert time.perf_counter() - t0 < 2.0


# ---- round 3: nested params, unbounded userinfo/name, default quote() form ----

@pytest.mark.parametrize("text,gone", [
    ("x?redirect_uri=https://app.example/cb?access_token=hunter2Secret!x&state=1", "hunter2Secret!x"),
    ("/login?next=/api/v1/export?api_key=Pa55word_99", "Pa55word_99"),
    ("?url=https://api.x.com/v1?token=s3cr3t-value", "s3cr3t-value"),
    ("?q=1?password=letmein123", "letmein123"),
    ("https://alice:" + "hunter2!" * 38 + "@host.example/x", "hunter2!hunter2!"),
    ("?" + "x" * 90 + "_token=ZZtop99secretVAL", "ZZtop99secretVAL"),
])
def test_redact_nested_and_long_forms(text, gone):
    assert gone not in sec.redact_patterns(text)


def test_redact_keeps_state_param_beside_nested_secret():
    out = sec.redact_patterns("?redirect_uri=https://a/cb?access_token=hunter2Secret!x&state=1")
    assert "state=1" in out


def test_redact_repeated_nested_params_neither_recurses_nor_is_slow():
    text = "?a=" * 33_000
    t0 = time.perf_counter()
    sec.redact_patterns(text)
    assert time.perf_counter() - t0 < 1.0


def test_scrub_default_quote_form():
    value = "ab/cd ef&gh"
    assert quote(value) != quote(value, safe="")
    assert scrub(f"echo {quote(value)} end", [value]) == f"echo {sec.HIDDEN} end"
