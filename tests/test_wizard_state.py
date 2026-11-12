from __future__ import annotations

import json
import stat

import pytest

from fakes import FakePlatform
from trading_agent.wizard.state import State, WizardState


def test_state_enum_is_exactly_the_planned_states():
    assert [s.name for s in State] == [
        "START", "PRECHECK", "SCAN", "AI_ROUTE", "AI_VERIFY", "BROKER_PICK", "BROKER_INSTALL",
        "BROKER_VERIFY", "SKILLS", "DATA_KEYS", "REVIEW", "WRITE", "VERIFY", "DONE"]


def test_roundtrip_and_permissions(tmp_path):
    fp = FakePlatform(tmp_path, config_home=tmp_path / "cfg")
    ws = WizardState(State.BROKER_PICK, {"profile_name": "trader", "declined:env:ALPACA_API_KEY": "1"})
    ws.save(fp)
    path = tmp_path / "cfg" / "trading-agent" / "state.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    again = WizardState.load(fp)
    assert again.current is State.BROKER_PICK
    assert again.decisions == ws.decisions
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]  # no temp file left behind


def test_load_missing_is_fresh(tmp_path):
    ws = WizardState.load(FakePlatform(tmp_path, config_home=tmp_path / "cfg"))
    assert ws.current is State.START and ws.decisions == {}


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"current": "NOPE", "decisions": {}}',
                                  '{"current": "SCAN", "decisions": []}',
                                  '{"current": "SCAN", "decisions": {"a": 1}}'])
def test_load_malformed_is_fresh_without_raising(tmp_path, text):
    path = tmp_path / "cfg" / "trading-agent" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    ws = WizardState.load(FakePlatform(tmp_path, config_home=tmp_path / "cfg"))
    assert ws.current is State.START and ws.decisions == {}


def test_set_refuses_values_that_could_not_be_a_decision(tmp_path):
    ws = WizardState()
    ws.set("ai.kind", "ollama")
    for bad in ("line\nbreak", "x" * 600, "ctl\x00"):
        with pytest.raises(ValueError) as e:
            ws.set("k", bad)
        assert bad not in str(e.value)
    with pytest.raises(ValueError):
        ws.set("bad key with spaces", "v")
    with pytest.raises(ValueError):
        ws.set("k", 5)  # type: ignore[arg-type]
    assert ws.decisions == {"ai.kind": "ollama"}


def test_file_content_is_plain_json(tmp_path):
    fp = FakePlatform(tmp_path, config_home=tmp_path / "cfg")
    WizardState(State.SCAN, {"a": "b"}).save(fp)
    data = json.loads((tmp_path / "cfg" / "trading-agent" / "state.json").read_text())
    assert data == {"current": "SCAN", "decisions": {"a": "b"}}
