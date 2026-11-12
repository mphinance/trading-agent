import dataclasses

from trading_agent.findings import Finding


def test_finding_has_no_value_field():
    assert {f.name for f in dataclasses.fields(Finding)} == {
        "id", "kind", "what", "where", "state", "names", "reusable",
    }
