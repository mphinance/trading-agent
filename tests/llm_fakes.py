"""DeterministicProvider: a single reusable fake for vesper.llm.call_openrouter.

Collapses the three ad hoc fake styles that used to coexist in
tests/test_llm_openrouter.py (env-var flipping via is_llm_enabled,
patch("httpx.AsyncClient.post", ...), and patch("vesper.llm.call_openrouter",
AsyncMock(...)) with .return_value / .side_effect) onto one mechanism.

Usage (from a test module in this directory -- pytest's rootless import mode
puts tests/ itself on sys.path, and "tests" is not usable as a package name
here because webull-openapi-mcp/tests/__init__.py already claims it):

    from llm_fakes import DeterministicProvider

    provider = DeterministicProvider(['{"passed": true, ...}'])
    monkeypatch.setattr("vesper.llm.call_openrouter", provider)

`generate_candidate_thesis` and `audit_proposal_risk` both call the
module-level `call_openrouter` name (not a bound/imported copy), so
monkeypatching it on the `vesper.llm` module reaches both call sites with
zero production-code changes.

Not used by any production code path -- vesper.py, morning.py, runner.py and
loop.py all only ever go through the real is_llm_enabled()/call_openrouter()
pair, so this lives in tests/, not vesper/llm.py.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class DeterministicProvider:
    """Async-callable fake matching call_openrouter's exact signature.

    Responses are popped off a queue in order, one per call -- so a
    single-response test passes `[json_str]` and a two-turn adversarial-loop
    test passes `[turn_1_json, turn_2_json]`, no `.side_effect` needed.

    Every call's arguments are recorded on `self.calls` as dicts, so
    assertions like `provider.calls[0]["model"] == "..."` or
    `provider.calls[-1]["messages"]` translate directly from the old
    `mock_call.call_args.kwargs["model"]` / `mock_call.call_args_list[1].args[0]`
    style.
    """

    def __init__(self, responses: Optional[List[Optional[str]]] = None) -> None:
        self._responses: List[Optional[str]] = list(responses or [])
        self.calls: List[Dict[str, Any]] = []

    async def __call__(
        self,
        messages: List[Dict[str, str]],
        model: str = "",
        temperature: float = 0.2,
        json_mode: bool = False,
        timeout_sec: float = 15.0,
    ) -> Optional[str]:
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "json_mode": json_mode,
                "timeout_sec": timeout_sec,
            }
        )
        if not self._responses:
            return None
        return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.calls)
