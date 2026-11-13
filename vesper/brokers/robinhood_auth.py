"""Robinhood MCP OAuth: file-backed token storage and a one-time login CLI.

Robinhood exposes trading as a hosted MCP server
(https://agent.robinhood.com/mcp/trading) behind standard OAuth: dynamic client
registration, PKCE (S256), public client, refresh tokens. Vesper logs in once,
stores the tokens here, and the MCP client refreshes them on its own.

    python -m vesper.brokers.robinhood_auth login

Tokens live OUTSIDE the repo (default ~/.vesper/robinhood_oauth.json, mode 0600;
override with VESPER_RH_TOKEN_PATH). They are credentials: never commit them.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

RH_MCP_URL = os.getenv("ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading")
CALLBACK_PORT = int(os.getenv("VESPER_RH_CALLBACK_PORT", "8765"))
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"


def token_path() -> Path:
    override = os.getenv("VESPER_RH_TOKEN_PATH")
    return Path(override) if override else Path.home() / ".vesper" / "robinhood_oauth.json"


class FileTokenStorage:
    """mcp.client.auth.TokenStorage backed by one JSON file."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or token_path()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, key: str, value: dict) -> None:
        data = self._read()
        data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)  # no-op on Windows; per-user profile dir there
        except OSError:
            pass

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        self._write("tokens", tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        self._write("client_info", client_info.model_dump(mode="json", exclude_none=True))

    def logged_in(self) -> bool:
        return bool(self._read().get("tokens"))


def build_provider(storage: Optional[FileTokenStorage] = None, *, interactive: bool = False):
    """OAuthClientProvider for the Robinhood MCP.

    interactive=False (the trading path): if tokens are missing or the refresh
    token is dead, the redirect handler raises instead of opening a browser, so
    an unattended run fails loudly rather than hanging on a login nobody sees.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    meta = OAuthClientMetadata(
        client_name="Vesper",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )

    async def _no_browser(_url: str) -> None:
        raise RuntimeError(
            "Robinhood login required. Run: python -m vesper.brokers.robinhood_auth login"
        )

    async def _no_callback():
        raise RuntimeError("Robinhood login required (non-interactive).")

    if not interactive:
        return OAuthClientProvider(
            RH_MCP_URL, meta, storage or FileTokenStorage(), _no_browser, _no_callback
        )

    result: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            q = parse_qs(urlparse(self.path).query)
            result["code"] = (q.get("code") or [None])[0]
            result["state"] = (q.get("state") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Vesper is logged in to Robinhood. You can close this tab.")

        def log_message(self, *_a):  # keep the auth code out of logs
            pass

    async def _redirect(url: str) -> None:
        print(f"Opening browser for Robinhood login:\n{url}\n")
        webbrowser.open(url)

    async def _callback():
        server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _Handler)
        await asyncio.to_thread(server.handle_request)
        server.server_close()
        if not result.get("code"):
            raise RuntimeError("Robinhood login returned no authorization code.")
        return result["code"], result.get("state")

    return OAuthClientProvider(
        RH_MCP_URL, meta, storage or FileTokenStorage(), _redirect, _callback
    )


async def _login() -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    storage = FileTokenStorage()
    provider = build_provider(storage, interactive=True)
    async with streamablehttp_client(RH_MCP_URL, auth=provider) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"Logged in. {len(tools.tools)} Robinhood tools available.")
            print(f"Tokens stored at {storage.path}")


def main(argv: list[str]) -> int:
    if argv[:1] == ["login"]:
        asyncio.run(_login())
        return 0
    if argv[:1] == ["status"]:
        print("logged in" if FileTokenStorage().logged_in() else "not logged in")
        return 0
    print("usage: python -m vesper.brokers.robinhood_auth [login|status]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
