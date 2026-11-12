from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from . import CatalogError


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    text = resources.files(__package__).joinpath("entry.schema.json").read_text(encoding="utf-8")
    schema = json.loads(text)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_entry(entry: dict[str, Any]) -> None:
    err = best_match(_validator().iter_errors(entry))
    if err is None:
        return
    slug = entry.get("slug", "<no slug>") if isinstance(entry, dict) else "<no slug>"
    path = ".".join(str(p) for p in err.absolute_path) or "<root>"
    raise CatalogError(f"{slug}: {path}: {err.message}")
