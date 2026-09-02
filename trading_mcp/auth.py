"""Constant-time token verification for trading_mcp's static-bearer auth path.

M2-02: fastmcp's own `StaticTokenVerifier` (`fastmcp.server.auth.providers.jwt`)
checks a presented bearer token with `self.tokens.get(token)` — a plain dict
lookup. CPython resolves a hash collision in that lookup with `str.__eq__`,
which short-circuits on the first differing byte and is therefore not
constant-time. For a single-operator bearer token that's a real (if narrow)
timing side-channel, so this module re-implements the same "does this string
match a configured token" check with `hmac.compare_digest`, which is built to
not leak comparison time.

`HmacStaticTokenVerifier` is otherwise a drop-in replacement for
`StaticTokenVerifier`: same constructor shape (`tokens: dict[token, metadata]`,
`required_scopes`), same `AccessToken` result. `trading_mcp/server.py`'s
`_build_auth()` is the only place this repo is meant to construct one — see
CLAUDE.md rule 3 / app_spec.txt I3 for why an MCP-side module never gets to
invent its own auth story wholesale, only harden the one path that exists.
"""

from __future__ import annotations

import hmac
import time
from typing import Any

from fastmcp.server.auth import AccessToken, TokenVerifier


class HmacStaticTokenVerifier(TokenVerifier):
    """Static-token verifier whose comparison is `hmac.compare_digest`.

    Every configured token is compared against the presented one — never
    short-circuited on the first match — so the total comparison time does
    not depend on which (or whether any) token matched.
    """

    def __init__(
        self,
        tokens: dict[str, dict[str, Any]],
        required_scopes: list[str] | None = None,
    ):
        super().__init__(required_scopes=required_scopes)
        self.tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        token_data: dict[str, Any] | None = None
        for candidate, data in self.tokens.items():
            # Iterate every candidate rather than returning on first hit —
            # a single-operator token set is tiny (usually one entry), so
            # this costs nothing and keeps timing independent of match order.
            if hmac.compare_digest(token, candidate):
                token_data = data

        if token_data is None:
            return None

        expires_at = token_data.get("expires_at")
        if expires_at is not None and expires_at < time.time():
            return None

        scopes = token_data.get("scopes", [])
        if self.required_scopes:
            if not set(self.required_scopes).issubset(set(scopes)):
                return None

        return AccessToken(
            token=token,
            client_id=token_data["client_id"],
            scopes=scopes,
            expires_at=expires_at,
        )
