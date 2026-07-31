"""Claude chat backend, built on the Claude Agent SDK (Claude Code as a library).

Credential resolution, in order:
  1. CLAUDE_CODE_OAUTH_TOKEN  — `claude setup-token`, runs under your Claude
     subscription rather than per-token API billing.
  2. ANTHROPIC_API_KEY        — pay-per-token API billing.

Both are read from ../.env.anthropic by run.sh. Note the Agent SDK docs state
that offering claude.ai login/rate limits *in a product for other users* needs
prior approval — the OAuth path is for personal use; ship with an API key.
"""

from __future__ import annotations

import os
from typing import AsyncIterator

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    query,
)

# Read-only. sidecar never places orders anywhere, and the chat panel is no
# exception — these are the only tools it ever gets.
ALLOWED_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]

# Sonnet 5, not Haiku: this panel reasons over the portfolio and calls tools,
# which is where Haiku is weakest. On CLAUDE_CODE_OAUTH_TOKEN there is no
# per-token cost at all, and on an API key the delta is ~1.3c vs 0.65c a turn —
# not worth a duller answer. Override with SIDECAR_CHAT_MODEL (e.g.
# claude-opus-4-8 for the hardest questions, claude-haiku-4-5 to economise).
DEFAULT_MODEL = os.environ.get("SIDECAR_CHAT_MODEL", "claude-sonnet-5")

SYSTEM_PROMPT = """You are the analyst panel inside `sidecar`, a read-only companion \
deck the user runs beside Webull Desktop. You cannot place trades — only discuss \
what the account already holds.

The user's live portfolio, guardrails, TraderDaddy Pro signals, and (when a \
symbol is in focus) its dealer-gamma structure are injected into each turn as a \
LIVE DATA block. Read it from there — do not try to fetch it. It is already \
current as of this turn.

Reading the DEALER GAMMA block, if present:
- It is a map of where option hedging is concentrated. It is NOT a forecast. \
Heavy gamma marks where price often REACTS on arrival; it never means price \
will travel there. A wall above spot is not a reason to be long.
- The flip is a regime boundary, not a target: above it dealer hedging dampens \
moves (fades, mean reversion), below it it amplifies them (trends, breakouts).
- If flip_split is true the two models disagree about which side price is on, \
so the regime call is genuinely uncertain — say so rather than picking one.
- Levels decay into expiry and the block lists which expirations it covers.

Some of the user's questions will arrive by voice, so they may be short, \
punctuated oddly, or contain a mis-transcribed ticker (speech recognition \
renders NVDA as "in video", and so on). If a symbol in the question is close to \
one in the LIVE DATA block, assume the block is right and say which you used.

The account is small (a few hundred dollars). Position sizing and downside \
matter far more than entry ideas; fixed costs and concentration hurt at this size.

How to answer:
- Lead with the answer. This renders in a narrow side panel — be brief and concrete.
- Cite the real numbers from the LIVE DATA block rather than speaking generally.
- You are not a financial adviser and the user is responsible for their own \
trades. Say what the data shows, flag risk plainly, and don't hedge every sentence.
- Never print API keys, tokens, or account numbers. The user streams this panel \
on video; treat everything you render as public.
"""


def _fmt_levels(lv: dict | None) -> list[str]:
    """Render one symbol's dealer-gamma structure as prompt lines.

    Reads td.levels()'s compacted shape, never a raw get_gex_ticker payload —
    that one carries the whole strike ladder (~40KB on SPY) and would dwarf the
    portfolio it is meant to inform.
    """
    if not lv or lv.get("error") or not lv.get("spot"):
        return []
    out = [f"DEALER GAMMA — {lv['symbol']} (spot ${lv['spot']:.2f}, as of {lv.get('as_of', '?')}):"]

    flip, regime = lv.get("flip"), lv.get("regime")
    if flip:
        side = "ABOVE" if lv.get("above_flip") else "BELOW"
        line = f"  flip=${flip:.2f} — price is {side} it ({regime or 'regime unknown'})"
        if lv.get("flip_split"):
            line += (f" [SPLIT: gex says ${lv['flip_gex']:.2f}, apex says ${lv['flip_apex']:.2f} — "
                     "the two models put price on opposite sides, so the regime call is uncertain]")
        out.append(line)
    if lv.get("pin"):
        out.append(f"  max-gamma pin=${lv['pin']:.2f}  net_gex={lv.get('net_gex', 0):,.0f}  "
                   f"put/call_gex={lv.get('pc_gex_ratio', 0):.2f}")

    kl = [f"${k['strike']:.2f} {k['type']}" for k in (lv.get("key_levels") or []) if k.get("strike")]
    if kl:
        out.append("  key levels: " + ", ".join(kl))

    for w in (lv.get("walls") or []):
        out.append(f"    ${w['strike']:.2f} {w['side']:<5} net_gex={w['net_gex']/1e6:+,.0f}M  "
                   f"call_oi={w['call_oi']:,.0f} put_oi={w['put_oi']:,.0f}")
    for a in (lv.get("apex") or []):
        out.append(f"    apex #{a['rank']}: ${a['strike']:.2f} score={a['score']:.0f}/100 oi={a['oi']:,.0f}")

    if lv.get("gamma_pocket"):
        out.append(f"  gamma pocket: {lv['gamma_pocket']} (positioning divergence from the broad "
                   "regime — a sizing/hedging note, not a direction call)")
    if lv.get("apex_note"):
        out.append(f"  note: {lv['apex_note']} — scored levels unavailable, flip is gex-only")
    if lv.get("expirations"):
        out.append("  expirations covered: " + ", ".join(lv["expirations"]))
    return out


def _fmt_context(portfolio: dict | None, signals: dict | None, levels: dict | None = None) -> str:
    """Compact the live state into a prompt block.

    Injected rather than fetched: the server already holds this data, WebFetch
    upgrades http:// to https:// (so it cannot read our loopback server), and a
    tool round-trip costs latency and tokens for data we have in hand.
    """
    if not portfolio:
        # Gamma alone is still worth a turn — the user may be asking about a
        # symbol they don't hold, or Webull may simply be rate-limited.
        gx = _fmt_levels(levels)
        return "\n".join(["<live_data>", *gx, "</live_data>"]) if gx else ""
    t = portfolio.get("totals", {})
    lines = [
        "<live_data>",
        f"ACCOUNT: net_liq=${t.get('nlv', 0):.2f} cash/buying_power=${t.get('buying_power', 0):.2f} "
        f"day_pl=${t.get('day_pl', 0):.2f} unrealized=${t.get('unrealized_pl', 0):.2f} "
        f"({t.get('unrealized_pl_pct', 0) * 100:+.1f}% on ${t.get('cost', 0):.2f} cost) "
        f"winners={t.get('winners', 0)}/{t.get('position_count', 0)}",
        "POSITIONS (symbol qty cost_basis last market_value unrealized pct):",
    ]
    for p in portfolio.get("positions", []):
        lines.append(
            f"  {p['symbol']:<6} {p['quantity']:<12.4f} {p['cost_price']:<9.4f} {p['last_price']:<9.4f} "
            f"${p['market_value']:<9.2f} ${p['unrealized_pl']:<9.2f} {p['unrealized_pl_pct'] * 100:+.1f}% "
            f"[{p['instrument_type']}]"
        )
    if portfolio.get("risk"):
        lines.append("GUARDRAILS (computed by this app):")
        for r in portfolio["risk"]:
            lines.append(f"  [{r['level'].upper()}] {r['name']}: {r['message']} — {r.get('detail', '')}")

    if signals and signals.get("configured"):
        st = signals.get("market_stats") or {}
        if st and "error" not in st:
            lines.append(
                f"OPTIONS FLOW: SPY p/c={st.get('spy_put_call_ratio', 0):.2f} ({st.get('spy_sentiment')}) "
                f"QQQ={st.get('qqq_put_call_ratio', 0):.2f} ({st.get('qqq_sentiment')}) "
                f"IWM={st.get('iwm_put_call_ratio', 0):.2f} ({st.get('iwm_sentiment')})"
            )
        h = signals.get("market_health") or {}
        if h and "error" not in h:
            cs = h.get("compositeScore") or {}
            lines.append(f"MARKET HEALTH: {cs.get('value')}/{cs.get('max')} signals tripped ({cs.get('label')})")
            for s in (h.get("signals") or []):
                if s.get("status") == "ALERT":
                    lines.append(f"  [ALERT] {s.get('category')} — {s.get('label')}: {s.get('summary')}")
        conv = signals.get("conviction") or {}
        if conv:
            bits = []
            for sym, c in conv.items():
                if isinstance(c, dict) and isinstance(c.get("score"), int):
                    pillars = len(c.get("breakdown") or {})
                    bits.append(f"{sym}={c['score']}" + ("(no signal)" if pillars <= 1 and c["score"] == 50 else ""))
            if bits:
                lines.append("TD CONVICTION (0-100, 50=neutral): " + " ".join(bits))

    lines.extend(_fmt_levels(levels))
    lines.append("</live_data>")
    return "\n".join(lines)


def credential() -> tuple[str | None, str]:
    """Return (kind, human_label) for whichever credential is available."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "oauth", "Claude subscription (CLAUDE_CODE_OAUTH_TOKEN)"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api_key", "API key (ANTHROPIC_API_KEY)"
    return None, "no credential — set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY in .env.anthropic"


def configured() -> bool:
    return credential()[0] is not None


async def ask(
    prompt: str,
    session_id: str | None = None,
    model: str | None = None,
    portfolio: dict | None = None,
    signals: dict | None = None,
    levels: dict | None = None,
) -> AsyncIterator[dict]:
    """Stream a chat turn as {type, ...} events for the UI.

    Yields: {"type":"session","id":...} | {"type":"text","text":...}
            | {"type":"tool","name":...} | {"type":"done","cost_usd":...,"session_id":...}
            | {"type":"error","message":...}
    """
    kind, label = credential()
    if kind is None:
        yield {"type": "error", "message": label}
        return

    ctx = _fmt_context(portfolio, signals, levels)
    full_prompt = f"{ctx}\n\n{prompt}" if ctx else prompt

    opts = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        allowed_tools=ALLOWED_TOOLS,
        model=model or DEFAULT_MODEL,
        # Don't inherit the user's ~/.claude config into a panel they stream.
        setting_sources=[],
        **({"resume": session_id} if session_id else {}),
    )

    try:
        async for msg in query(prompt=full_prompt, options=opts):
            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                sid = (msg.data or {}).get("session_id")
                if sid:
                    yield {"type": "session", "id": sid}

            elif isinstance(msg, AssistantMessage):
                # The SDK reports auth failures as a synthetic assistant message
                # with .error set — surface it instead of rendering it as chat.
                err = getattr(msg, "error", None)
                if err:
                    text = next((b.text for b in msg.content if isinstance(b, TextBlock)), str(err))
                    yield {"type": "error", "message": f"{err}: {text}"}
                    return
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        yield {"type": "text", "text": block.text}
                    elif getattr(block, "name", None):
                        yield {"type": "tool", "name": block.name}

            elif isinstance(msg, ResultMessage):
                yield {
                    "type": "done",
                    "cost_usd": getattr(msg, "total_cost_usd", None),
                    "session_id": getattr(msg, "session_id", None),
                    "is_error": getattr(msg, "is_error", False),
                }
    except Exception as e:
        # The SDK can raise "returned an error result: success" on an auth
        # failure — useless on its own, so name the credential in play.
        yield {"type": "error", "message": f"{type(e).__name__}: {e} (using {label})"}
