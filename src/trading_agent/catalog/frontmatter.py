from __future__ import annotations

import re

from . import CatalogError

# Mirrors frontmatter() in the catalog's own scripts (check-freshness.mjs,
# check-upstream.mjs). Deliberately not a YAML library: the front matter is flat.
_BLOCK = re.compile(r"^---\r?\n(.*?)\r?\n---", re.DOTALL)
_KV = re.compile(r"^([A-Za-z_]+):\s*(.*)$")
_COMMENT = re.compile(r"\s+#.*$")


def parse_front_matter(text: str, source: str = "<text>") -> dict[str, str]:
    m = _BLOCK.match(text)
    if not m:
        raise CatalogError(f"{source}: no front matter block")
    out: dict[str, str] = {}
    for line in re.split(r"\r?\n", m.group(1)):
        kv = _KV.match(line)
        if kv:
            out[kv.group(1)] = _COMMENT.sub("", kv.group(2)).strip()
    return out
