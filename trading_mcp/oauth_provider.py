"""trading_mcp.oauth_provider: a single-operator OAuth 2.1 authorization server.

M2-03. This exists so claude.ai can add the `trading-agent` connector through
the normal "Connect" button instead of a pasted `Authorization: Bearer ...`
header, which app_spec.txt (decision 4) rejects as the *only* path because
Anthropic's request-headers connector auth is beta and has open reports of
the header silently not being sent. The static bearer (`trading_mcp/auth.py`)
stays as the documented fallback — see `_build_auth()` in `server.py`.

THERE IS NO USER MODEL HERE, on purpose (CLAUDE.md: "single-operator personal
tool... no authentication, no multi-tenancy, no user model anywhere in this
codebase"). A textbook OAuth Authorization Server answers "which user, with
what consent"; this one only ever answers one question: "does this request
carry the credential Michael provisioned" (`TRADING_AGENT_TOKEN`, reused here
as `operator_secret` rather than inventing a second secret). Concretely:

- Dynamic Client Registration (RFC 7591) stays OPEN. Gating `/register`
  behind a header would just re-create the "paste a header" problem OAuth was
  chosen to avoid, and DCR alone hands out no access — a registered client
  still cannot obtain a token.
- The credential check happens at the one place a human is actually present:
  `/authorize`. `SingleOperatorOAuthProvider.authorize()` is NEVER reached
  without the caller first presenting `operator_secret` via the gate in
  `get_routes()` — a plain HTML form requiring it, checked with
  `hmac.compare_digest`. Read `docs/AUTH_TRADE_SCOPE_LOCKDOWN.md` and
  `docs/HANDOFF_2026-09-01.md` before touching this file: supermcp shipped
  exactly the bug this design is built to avoid — `authorize()` force-granting
  admin on every OAuth handshake, i.e. treating "an OAuth request arrived" as
  itself sufficient authorization. Here, reaching `authorize()` at all already
  implies the gate passed; `authorize()` itself only refuses an unregistered
  `client_id`, it does not perform its own separate credential check. Do not
  "simplify" that split back into one undifferentiated grant, and do not move
  the credential check into `authorize()` where a route that forgets to wrap
  `/authorize` (a new mount point, a refactor) could silently skip it.

STORAGE: clients and auth codes live in plain in-memory dicts and are lost
on restart -- deliberately, see __init__'s comment for why that's fine.
Access and refresh tokens are different: M2-09 persists those to
`data/oauth_tokens_state.json` (same atomic-write, 0600, gitignored-outside-
git pattern as core/halt.py's state file, registered in tests/conftest.py's
`_isolated_vesper_state` autouse fixture), because a token issued to a real
client has to survive this process restarting, and a revoked token has to
stay revoked after it does. See `_load_token_state()` / `_save_token_state()`
/ `_persist_tokens()` below.

No tool call, no broker reference: this module is pure OAuth bookkeeping. The
rule-3 AST pin in `tests/test_trading_mcp.py` walks every file under
`trading_mcp/`, so this module is covered automatically, not by inspection.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from fastmcp.server.auth.auth import (
    ClientRegistrationOptions,
    OAuthProvider,
    RevocationOptions,
)

logger = logging.getLogger(__name__)

# M2-09: persisted access/refresh token store. Same pattern as every other
# state file in this repo (core/halt.py, core/approval_registry.py, ...):
# a hardcoded module-level path under the repo-root data/ dir, loaded fresh
# and written back atomically (temp file + os.replace) rather than edited
# in place. tests/conftest.py's autouse `_isolated_vesper_state` fixture
# monkeypatches both constants below to a per-test tmp_path, the same way
# it already does for core.halt._DATA_DIR etc. -- do not read this module's
# data as "vesper state" just because that fixture's name says vesper; the
# fixture already covers non-vesper modules (core.metrics, core.approval_
# registry) for the identical cross-test-contamination reason.
#
# Deliberately NOT reusing core/halt.py's _DATA_DIR constant directly: this
# module must stay import-time-coupled to nothing but the stdlib (M2-10),
# and defining its own Path(__file__)-relative constant, rather than
# `from core.halt import _DATA_DIR`, keeps that true without relying on
# core/ happening to also be dependency-free today.
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_TOKEN_STATE_PATH = _DATA_DIR / "oauth_tokens_state.json"


def _load_token_state() -> dict[str, Any]:
    """Read the persisted access/refresh token store, or an empty one if it
    doesn't exist yet or fails to parse (corrupt/partial write) -- same
    fail-open-to-empty shape as core/halt.py's `_load_state`. An unreadable
    token file must never crash server startup; it just means every
    previously issued token is treated as gone, which is the safe direction
    to fail (a client that lost its token re-authorizes; a client that kept
    a token nobody meant to revoke is the actual danger, and this can't
    cause that)."""
    empty: dict[str, Any] = {
        "access_tokens": {},
        "refresh_tokens": {},
        "access_to_refresh": {},
        "refresh_to_access": {},
    }
    if not _TOKEN_STATE_PATH.exists():
        return empty
    try:
        with open(_TOKEN_STATE_PATH) as f:
            state = json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read OAuth token state file: {e}")
        return empty
    for key in empty:
        state.setdefault(key, {})
    return state


def _save_token_state(state: dict[str, Any]) -> None:
    """Atomic write (temp file + os.replace, same as core/halt.py's
    `_save_state`) with owner-only permissions -- these are live bearer
    credentials, the same standing this repo holds .env to (CLAUDE.md rule
    2). `os.chmod` runs on the temp file BEFORE `os.replace`: on POSIX,
    rename preserves the source inode's mode, so the final path inherits
    0600 too; chmod-ing only after the replace would leave a window where
    the real path existed briefly with the umask's default (group/other
    readable) permissions."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _TOKEN_STATE_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, _TOKEN_STATE_PATH)


# Default expiries. Access tokens are short-lived; refresh tokens are long
# because this is a phone-in-your-pocket connector Michael reconnects to
# repeatedly, not a browser session that logs out nightly.
AUTH_CODE_EXPIRY_SECONDS = 5 * 60
ACCESS_TOKEN_EXPIRY_SECONDS = 60 * 60
REFRESH_TOKEN_EXPIRY_SECONDS = 30 * 24 * 60 * 60

# The query/form field name the gate form and the gate check use for the
# shared credential. Deliberately not "token" or "secret" alone — grep-able,
# and distinct from the OAuth spec's own `client_secret` field.
_GATE_FIELD = "operator_key"

# Query/form fields belonging to the OAuth authorization request itself,
# carried through the gate form as hidden fields so a submit doesn't lose them.
_OAUTH_PASSTHROUGH_FIELDS = (
    "response_type", "client_id", "redirect_uri", "scope",
    "state", "code_challenge", "code_challenge_method", "resource",
)


def _lookup_constant_time(mapping: dict[str, Any], presented: str) -> Any | None:
    """Find `presented` among `mapping`'s keys via `hmac.compare_digest`
    against every key, rather than `mapping.get(presented)`.

    `dict.get` resolves a hash collision with `str.__eq__`, which
    short-circuits on the first differing byte — the same non-constant-time
    leak `trading_mcp/auth.py`'s `HmacStaticTokenVerifier` docstring explains
    for the static bearer token, and it applies equally here: an OAuth
    access token, refresh token or authorization code is exactly the kind of
    bearer secret that check exists to protect. A single-operator token set
    is tiny, so iterating every entry (rather than stopping at the first
    match) costs nothing and keeps timing independent of which, or whether
    any, key matched.
    """
    match = None
    for candidate, value in mapping.items():
        if hmac.compare_digest(presented, candidate):
            match = value
    return match


class SingleOperatorOAuthProvider(OAuthProvider):
    """In-memory OAuth 2.1 authorization server gated by one shared secret.

    Drop-in for `fastmcp`'s `auth=` parameter: `FastMCP("name", auth=this)`
    mounts `/authorize`, `/token`, `/register`, `/.well-known/oauth-
    authorization-server` (and the OIDC alias) automatically via
    `OAuthProvider.get_routes()` / `get_well_known_routes()`. This subclass
    only overrides `get_routes()` to wrap `/authorize` with the operator-key
    gate described in the module docstring.
    """

    def __init__(
        self,
        *,
        operator_secret: str,
        base_url: Any,
        required_scopes: list[str] | None = None,
        valid_scopes: list[str] | None = None,
        default_scopes: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """`required_scopes`, `valid_scopes` and `default_scopes` are three
        different things, and conflating the first two was a real bug.

        - `required_scopes` -- the MINIMUM every accepted token must carry.
        - `valid_scopes`    -- the MAXIMUM a DCR client may register for.
        - `default_scopes`  -- what a client that names no scope gets.

        Until M8-24 this constructor took only `required_scopes` and passed
        that same list as `valid_scopes`. Production calls it with
        `["read"]`, so the registerable set collapsed to `{"read"}` and no
        credential this server could issue would ever satisfy
        `order_tools.py`'s `require_scopes("trade")`. That failed CLOSED, so
        it was safe while the order tools were unregistered -- but it made
        wiring them in impossible without also locking the owner out. The
        fix belongs here, in the plumbing, never in a weakened
        `require_scopes`.
        """
        if not operator_secret:
            raise ValueError("operator_secret must be non-empty")

        client_registration_options = kwargs.pop(
            "client_registration_options", None
        ) or ClientRegistrationOptions(
            enabled=True,
            valid_scopes=valid_scopes or ["read", "safe-write", "trade"],
            default_scopes=default_scopes or ["read"],
        )
        revocation_options = kwargs.pop(
            "revocation_options", None
        ) or RevocationOptions(enabled=True)

        super().__init__(
            base_url=base_url,
            required_scopes=required_scopes or ["read"],
            client_registration_options=client_registration_options,
            revocation_options=revocation_options,
            **kwargs,
        )
        self._operator_secret = operator_secret

        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}

        # M2-09: access/refresh tokens are loaded from the on-disk store
        # built above, not started empty -- a restart must not silently
        # forget every token a client is still holding, and a token
        # revoked before the restart must not be resurrected by it.
        # Clients and auth codes stay in-memory only, deliberately: a
        # client that fails to re-register after a restart just re-runs
        # DCR (cheap and deliberately open, see the module docstring), and
        # an authorization code is single-use and expires in 5 minutes --
        # not worth the persistence surface.
        _persisted = _load_token_state()
        self.access_tokens: dict[str, AccessToken] = {
            token: AccessToken(**data)
            for token, data in _persisted["access_tokens"].items()
        }
        self.refresh_tokens: dict[str, RefreshToken] = {
            token: RefreshToken(**data)
            for token, data in _persisted["refresh_tokens"].items()
        }
        self._access_to_refresh: dict[str, str] = dict(_persisted["access_to_refresh"])
        self._refresh_to_access: dict[str, str] = dict(_persisted["refresh_to_access"])

    # ── Dynamic Client Registration (RFC 7591) — stays open, see module doc ──

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise ValueError("client_id is required for client registration")
        self.clients[client_info.client_id] = client_info

    # ── The credential gate: wraps /authorize only ──────────────────────────

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        sdk_handler = AuthorizationHandler(self)
        gated: list[Route] = []
        for route in routes:
            if isinstance(route, Route) and route.path == "/authorize":
                gated.append(
                    Route(
                        "/authorize",
                        endpoint=self._gated_authorize(sdk_handler),
                        methods=["GET", "POST"],
                    )
                )
            else:
                gated.append(route)

        # RFC 9728 follow-up: FastMCP mounts /.well-known/oauth-protected-resource{mcp_path}.
        # RFC 9728 clients and discovery checks also query the root
        # /.well-known/oauth-protected-resource directly. Alias it to the same endpoint.
        for route in list(gated):
            if isinstance(route, Route) and route.path.startswith("/.well-known/oauth-protected-resource/"):
                gated.append(
                    Route(
                        "/.well-known/oauth-protected-resource",
                        endpoint=route.endpoint,
                        methods=route.methods,
                    )
                )
                break

        return gated

    def _gated_authorize(self, sdk_handler: AuthorizationHandler):
        async def endpoint(request: Request) -> Response:
            if request.method == "GET":
                params = request.query_params
            else:
                params = await request.form()

            supplied = params.get(_GATE_FIELD)
            if isinstance(supplied, str) and supplied and hmac.compare_digest(
                supplied, self._operator_secret
            ):
                # Gate passed. Only now does this request reach
                # AuthorizationHandler.handle(), which is the only caller of
                # self.authorize() anywhere in this class.
                return await sdk_handler.handle(request)

            # No credential, or the wrong one: never call self.authorize(),
            # never mint a code. Re-render the form (with an error if a
            # (wrong) value was actually submitted, vs. the first GET).
            wrong_attempt = supplied is not None
            passthrough = {
                field: v
                for field in _OAUTH_PASSTHROUGH_FIELDS
                if (v := params.get(field)) is not None
            }
            return self._render_gate_form(passthrough, wrong_attempt=wrong_attempt)

        return endpoint

    def _render_gate_form(
        self, passthrough: dict[str, str], *, wrong_attempt: bool
    ) -> HTMLResponse:
        """Render the operator gate.

        The form POSTs, and that is not cosmetic. Submitting by GET puts
        `operator_key=<TRADING_AGENT_TOKEN>` — the same secret that is the
        static bearer — into the request line, where Traefik's and uvicorn's
        access logs and the browser's own history keep it verbatim, on every
        ordinary reconnect, with no attacker involved. `_gated_authorize()`
        reads GET params too, so the initial redirect from a client still
        works; only the step that carries the secret is POSTed.

        The form also names the client and scope being authorised, because a
        gate that shows the human nothing about what they are approving is a
        confused-deputy waiting for the day a scope beyond `read` goes live.
        """
        hidden_inputs = "\n".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}">'
            for k, v in passthrough.items()
        )
        error_html = (
            '<p style="color:#b00">Wrong key. Try again.</p>' if wrong_attempt else ""
        )
        requested_client = html.escape(str(passthrough.get("client_id", "(unnamed)")))
        requested_scope = html.escape(str(passthrough.get("scope", "(none requested)")))
        request_html = (
            f"<p>Client <code>{requested_client}</code> is requesting scope "
            f"<code>{requested_scope}</code>.</p>"
        )
        body = f"""<!doctype html>
<html><head><title>trading-agent authorization</title></head>
<body>
<h1>trading-agent</h1>
<p>Owner-only connector. Enter the operator key to continue.</p>
{request_html}
{error_html}
<form method="post" action="/authorize">
{hidden_inputs}
<input type="password" name="{_GATE_FIELD}" autofocus>
<button type="submit">Authorize</button>
</form>
</body></html>"""
        status = 401 if wrong_attempt else 200
        return HTMLResponse(body, status_code=status)

    # ── Authorization code issuance — only reachable past the gate above ───

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if client.client_id not in self.clients:
            raise AuthorizeError(
                error="unauthorized_client",
                error_description=f"Client {client.client_id!r} is not registered.",
            )

        # M2-06: this is the exact function whose supermcp counterpart
        # force-granted admin scope on every OAuth handshake — treating "an
        # authorization request arrived, naming some scope" as itself
        # sufficient to grant that scope, rather than checking it against
        # what the caller was actually entitled to. `params.scopes` is
        # attacker-controlled input (a query/form field on `/authorize`);
        # `client.scope` is what THIS client was actually registered for,
        # itself already bounded to `valid_scopes` at registration time by
        # the SDK's `RegistrationHandler` (`ClientRegistrationOptions` in
        # `__init__` above). The intersection below is the escalation
        # guard: a requested scope never in the client's registration is
        # silently dropped, never honoured, no matter how it got here. The
        # mcp SDK's `AuthorizationHandler` already rejects an out-of-scope
        # request before this method is even called (redirects with
        # `error=invalid_scope`), but that upstream check is a second layer,
        # not a substitute for this one — this method must never assume a
        # caller reached it only through that front door. Do not replace
        # this filter with an unconditional `scopes_list = params.scopes`;
        # that is precisely supermcp's bug, reproduced here.
        # Pinned by tests/test_trading_mcp.py's
        # test_authorize_itself_filters_scope_beyond_client_registration
        # (calls this method directly, bypassing the SDK's own upstream
        # check, to prove this line — not just the layer in front of it —
        # is what stops the escalation) and
        # test_authorize_request_for_unregistered_scope_never_issues_a_code
        # (the end-to-end HTTP path).
        scopes_list = params.scopes or []
        if client.scope:
            allowed = set(client.scope.split())
            scopes_list = [s for s in scopes_list if s in allowed]

        code_value = f"toa_code_{secrets.token_urlsafe(32)}"
        auth_code = AuthorizationCode(
            code=code_value,
            client_id=client.client_id,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            scopes=scopes_list,
            expires_at=time.time() + AUTH_CODE_EXPIRY_SECONDS,
            code_challenge=params.code_challenge,
        )
        self.auth_codes[code_value] = auth_code
        return construct_redirect_uri(
            str(params.redirect_uri), code=code_value, state=params.state
        )

    # ── Token issuance / refresh / verification ─────────────────────────────

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = _lookup_constant_time(self.auth_codes, authorization_code)
        if code is None:
            return None
        if code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            del self.auth_codes[code.code]
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.code not in self.auth_codes:
            raise TokenError("invalid_grant", "Authorization code not found or already used.")
        del self.auth_codes[authorization_code.code]  # single-use

        return self._issue_token_pair(client.client_id, authorization_code.scopes)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token_obj = _lookup_constant_time(self.refresh_tokens, refresh_token)
        if token_obj is None:
            return None
        if token_obj.client_id != client.client_id:
            return None
        if token_obj.expires_at is not None and token_obj.expires_at < time.time():
            self._revoke_pair(refresh_token_str=token_obj.token)
            return None
        return token_obj

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        original = set(refresh_token.scopes)
        requested = set(scopes)
        if not requested.issubset(original):
            raise TokenError(
                "invalid_scope",
                "Requested scopes exceed those authorized by the refresh token.",
            )
        self._revoke_pair(refresh_token_str=refresh_token.token)  # rotate
        return self._issue_token_pair(client.client_id, scopes)

    def _persist_tokens(self) -> None:
        """Write the current access/refresh token dicts to disk (M2-09).
        Called after every mutation -- issue, explicit revoke, and the
        expiry-driven prunes in load_access_token/load_refresh_token, all
        of which funnel through _issue_token_pair or _revoke_pair below --
        so the on-disk file is never stale relative to what this instance
        would answer a token check with, and a revocation survives this
        process exiting."""
        _save_token_state({
            "access_tokens": {
                token: obj.model_dump() for token, obj in self.access_tokens.items()
            },
            "refresh_tokens": {
                token: obj.model_dump() for token, obj in self.refresh_tokens.items()
            },
            "access_to_refresh": dict(self._access_to_refresh),
            "refresh_to_access": dict(self._refresh_to_access),
        })

    def _issue_token_pair(self, client_id: str, scopes: list[str]) -> OAuthToken:
        access_value = f"toa_at_{secrets.token_urlsafe(32)}"
        refresh_value = f"toa_rt_{secrets.token_urlsafe(32)}"
        access_expires_at = int(time.time() + ACCESS_TOKEN_EXPIRY_SECONDS)
        refresh_expires_at = int(time.time() + REFRESH_TOKEN_EXPIRY_SECONDS)

        self.access_tokens[access_value] = AccessToken(
            token=access_value, client_id=client_id, scopes=scopes,
            expires_at=access_expires_at,
        )
        self.refresh_tokens[refresh_value] = RefreshToken(
            token=refresh_value, client_id=client_id, scopes=scopes,
            expires_at=refresh_expires_at,
        )
        self._access_to_refresh[access_value] = refresh_value
        self._refresh_to_access[refresh_value] = access_value
        self._persist_tokens()

        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_EXPIRY_SECONDS,
            refresh_token=refresh_value,
            scope=" ".join(scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        token_obj = _lookup_constant_time(self.access_tokens, token)
        if token_obj is None:
            return None
        if token_obj.expires_at is not None and token_obj.expires_at < time.time():
            self._revoke_pair(access_token_str=token_obj.token)
            return None
        return token_obj

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    def _revoke_pair(
        self, *, access_token_str: str | None = None, refresh_token_str: str | None = None
    ) -> None:
        if access_token_str is not None:
            self.access_tokens.pop(access_token_str, None)
            paired_refresh = self._access_to_refresh.pop(access_token_str, None)
            if paired_refresh is not None:
                self.refresh_tokens.pop(paired_refresh, None)
                self._refresh_to_access.pop(paired_refresh, None)

        if refresh_token_str is not None:
            self.refresh_tokens.pop(refresh_token_str, None)
            paired_access = self._refresh_to_access.pop(refresh_token_str, None)
            if paired_access is not None:
                self.access_tokens.pop(paired_access, None)
                self._access_to_refresh.pop(paired_access, None)

        if access_token_str is not None or refresh_token_str is not None:
            # M2-09: persist the revocation (or expiry-driven prune) so it
            # survives this process exiting -- a bare in-memory pop is not
            # what "revocable" means once tokens are meant to outlive a
            # restart. Runs even when neither pop above actually found
            # anything (an unknown token, a double revoke): harmless and
            # keeps this branch simple rather than tracking whether the
            # pops changed anything.
            self._persist_tokens()

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._revoke_pair(access_token_str=token.token)
        elif isinstance(token, RefreshToken):
            self._revoke_pair(refresh_token_str=token.token)
