"""Hash-chained, append-only, tamper-evident audit ledger.

Scope: post-trade, append-only, non-blocking. Nothing here ever raises into
or is awaited by the execution path -- a chain-write failure is logged and
swallowed by the caller (see `vesper/graph.py`'s `_with_audit_chain`), never
surfaced as a rejection. This module only ever OBSERVES `audit_trail`
entries that each node already produces; it never gates or alters them.

Each record carries `prev_hash` (the previous record's own `hash`, or
GENESIS_HASH for the first record) and its own `hash` (a SHA-256 digest over
everything except `hash` itself). Editing, deleting, reordering, or
inserting a record breaks this chain in a way `verify_chain()` detects and
localizes -- see that function's docstring for exactly how.

Mirrors `vesper/halt.py`'s module shape (module-level `_DATA_DIR`/path
constants so tests/conftest.py can monkeypatch them, same as halt.py's
`_DATA_DIR`/`_HALT_STATE_PATH`) but append-mode + flock instead of halt.py's
tmp-file+os.replace whole-file rewrite -- see ROADMAP.md's Module entry for
why: this file grows by appending small records many times per session,
never rewrites an existing record, so a whole-file rewrite on every append
would be pure O(n) waste with no consistency benefit, whereas
tmp+os.replace exists specifically to make a small mutable JSON *object*
safe to fully overwrite (halt.py/paper_ledger.py's pattern). flock closes
the one real gap plain append-mode alone would leave: `vesper loop`'s
long-running daemon and a one-shot `vesper scan`/`vesper analyze` CLI
invocation can run concurrently on the same box and both open this file.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CHAIN_PATH = _DATA_DIR / "audit_chain.jsonl"

# 64 hex chars = the length of a SHA-256 digest, same convention as Bitcoin's
# genesis block's previous-hash field. A documented constant, not a
# market-data value -- rule 1 (never fabricate market data) is not in play.
GENESIS_HASH = "0" * 64


def _digest(payload: Any) -> str:
    """Same 2-line formula as `vesper/execution_guard.py`'s `_digest`,
    reimplemented rather than imported: that name is underscore-private and
    execution_guard.py is off-limits to edits, so importing (or exporting)
    a private symbol from it isn't available. Duplicating two lines is the
    honest choice over reaching into a module this batch must not modify.
    """
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def append_entry(session_id: str, node: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Append one audit_trail entry to the hash chain and return the record
    that was written.

    Never raises past a caller that wraps this in try/except -- disk-full,
    permissions, or any other I/O failure should be logged and swallowed by
    the caller (see graph.py's `_with_audit_chain`), not surfaced as an
    execution-path rejection. This function itself does not swallow errors;
    the "reports, never refuses" property is the caller's job so that a test
    can also exercise `append_entry` failing loudly in isolation.
    """
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CHAIN_PATH, "a+b") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            prev_hash = GENESIS_HASH
            index = 0
            last_line = None
            for raw_line in f:
                raw_line = raw_line.strip()
                if raw_line:
                    last_line = raw_line
            if last_line is not None:
                last_record = json.loads(last_line)
                prev_hash = last_record["hash"]
                index = last_record["index"] + 1

            record = {
                "index": index,
                "session_id": session_id,
                "node": node,
                "entry": entry,
                "prev_hash": prev_hash,
            }
            record["hash"] = _digest(record)
            record["recorded_at"] = datetime.now(timezone.utc).isoformat()

            line = json.dumps(record, sort_keys=True, default=str) + "\n"
            f.write(line.encode())
            f.flush()
            os.fsync(f.fileno())
            return record
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def verify_chain() -> Dict[str, Any]:
    """Walk the chain in order and verify every link.

    On first problem, stops and returns:
        {"valid": False, "break_index": i, "break_node": ..., "break_session": ...,
         "break_reason": "<human-readable reason>"}

    break_reason is one of:
      - "line N is not valid JSON"
      - "entry N's index claims M (file position/index mismatch, lines reordered)"
      - "entry 0's prev_hash != GENESIS_HASH (true first entry deleted)"
      - "entry N's prev_hash != entry N-1's hash (an entry was inserted, deleted, or reordered)"
      - "entry N's stored hash != recomputed hash (this entry's own content was edited after being written)"

    If every line checks out (or the file is empty/missing):
        {"valid": True, "entry_count": n}
    """
    if not _CHAIN_PATH.exists():
        return {"valid": True, "entry_count": 0}

    prev_hash = GENESIS_HASH
    count = 0
    with open(_CHAIN_PATH, "rb") as f:
        for line_no, raw_line in enumerate(f):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {
                    "valid": False,
                    "break_index": line_no,
                    "break_node": None,
                    "break_session": None,
                    "break_reason": f"line {line_no} is not valid JSON",
                }

            if count == 0:
                if record.get("prev_hash") != GENESIS_HASH:
                    return {
                        "valid": False,
                        "break_index": 0,
                        "break_node": record.get("node"),
                        "break_session": record.get("session_id"),
                        "break_reason": "entry 0's prev_hash != GENESIS_HASH (true first entry deleted)",
                    }
            elif record.get("prev_hash") != prev_hash:
                return {
                    "valid": False,
                    "break_index": count,
                    "break_node": record.get("node"),
                    "break_session": record.get("session_id"),
                    "break_reason": (
                        f"entry {count}'s prev_hash != entry {count - 1}'s hash "
                        "(an entry was inserted, deleted, or reordered between them)"
                    ),
                }

            stored_hash = record.get("hash")
            recomputed = {k: v for k, v in record.items() if k not in ("hash", "recorded_at")}
            if stored_hash != _digest(recomputed):
                return {
                    "valid": False,
                    "break_index": count,
                    "break_node": record.get("node"),
                    "break_session": record.get("session_id"),
                    "break_reason": (
                        f"entry {count}'s stored hash != recomputed hash "
                        f"(this entry's own content was edited after being written, "
                        f"node={record.get('node')} session={record.get('session_id')})"
                    ),
                }

            # Checked last, after the hash/prev_hash checks above: `index` is
            # itself one of the hashed fields, so any tampering that touches
            # it is already caught by the hash comparison above, and simple
            # physical-line reordering is already caught by the prev_hash
            # comparison (a record's prev_hash is tied to its place in the
            # real chain, not its position in the file). This check only
            # exists as an extra structural diagnostic for the residual case
            # where content and chain both still validate but position drifted.
            if record.get("index") != count:
                return {
                    "valid": False,
                    "break_index": count,
                    "break_node": record.get("node"),
                    "break_session": record.get("session_id"),
                    "break_reason": (
                        f"entry {count}'s index claims {record.get('index')} "
                        "(file position/index mismatch, lines reordered)"
                    ),
                }

            prev_hash = stored_hash
            count += 1

    return {"valid": True, "entry_count": count}
