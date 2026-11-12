from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Mapping, Sequence

import yaml

from trading_agent.findings import Finding
from trading_agent.platform import Platform

_DATA_PKG = "trading_agent"
_DATA_FILE = "credential_names.yaml"
_KINDS = {"ai-key", "broker-key", "data-key"}
_REUSABLE = {"reference-only", "copy-with-consent"}


@dataclass(frozen=True)
class CredentialSpec:
    finding_prefix: str
    kind: str
    what: str
    names: tuple[str, ...]
    reusable: str


def _load_doc() -> dict:
    text = (resources.files(_DATA_PKG) / "data" / _DATA_FILE).read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{_DATA_FILE}: expected a mapping at top level")
    return doc


def load_credential_specs() -> list[CredentialSpec]:
    specs = []
    for row in _load_doc().get("specs") or []:
        spec = CredentialSpec(
            finding_prefix=row["finding_prefix"], kind=row["kind"], what=row["what"],
            names=tuple(row["names"]), reusable=row["reusable"],
        )
        if spec.kind not in _KINDS or spec.reusable not in _REUSABLE:
            raise ValueError(f"{_DATA_FILE}: bad kind or reusable for {spec.what!r}")
        specs.append(spec)
    return specs


def load_info_names() -> list[str]:
    """Mode and guardrail switch names (information only, never findings)."""
    return [str(n) for n in _load_doc().get("info_names") or []]


def _is_set(environ: Mapping[str, str], name: str) -> bool:
    """True if NAME is present with a non-empty value. The only place a value is touched."""
    return bool(environ.get(name))


def detect_env(platform: Platform, catalog: Sequence[CredentialSpec]) -> list[Finding]:
    """One Finding per catalog name. Presence only; values never leave _is_set."""
    environ = platform.environ()
    findings: list[Finding] = []
    for spec in catalog:
        for name in spec.names:
            if _is_set(environ, name):
                state, where = "present", "environment variable"
            elif platform.login_shell_has_var(name):
                state, where = "present", "login shell environment"
            else:
                state, where = "absent", "environment variable"
            findings.append(Finding(
                id=f"{spec.finding_prefix}:{name}", kind=spec.kind, what=spec.what,
                where=where, state=state, names=(name,), reusable=spec.reusable,
            ))
    return findings
