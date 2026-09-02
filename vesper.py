#!/usr/bin/env python3
"""VESPER ⚡ Stateful Quantitative Trading & Execution Engine.

Synthesized with LangGraph, TraderDaddy Pro, Webull OpenAPI, TickerTrace, and VoPR™.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from vesper.runner import run_agent_session


def main():
    parser = argparse.ArgumentParser(
        description="VESPER ⚡ Stateful Quant Trading & Execution Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python vesper.py                      # Multi-source setup discovery (dry-run)
  python vesper.py scan                 # Scan across VCP, Squeeze, Institutional Flow
  python vesper.py analyze NVDA         # Deep technical & VoPR™ options audit for NVDA
  python vesper.py 0dte                 # 0DTE SPY/QQQ Gamma Flip decision support
  python vesper.py --playbook squeeze   # Trigger Momentum Volatility Squeeze playbook
  python vesper.py analyze AAPL --live  # Live Webull order execution (with safety gate)
  python vesper.py listen               # Long-poll Telegram for Approve/Reject/halt/resume taps
  python vesper.py loop                 # Unattended: scheduled scans + continuous position monitor
  python vesper.py loop --live          # Same, but drafts pause for remote approval (run `listen` too)
        """
    )
    
    parser.add_argument(
        "command",
        nargs="?",
        default="scan",
        choices=["scan", "analyze", "0dte", "morning", "monitor", "halt", "resume", "status", "paper", "listen", "loop", "alerts", "audit"],
        help="Action command",
    )
    parser.add_argument("ticker", nargs="?", default=None, help="Target symbol for analysis")
    parser.add_argument(
        "--playbook", default="all",
        choices=[
            "all", "momentum_squeeze", "0dte_flow", "institutional_convergence",
            "collar_following", "adx_iv_router", "thega", "recycle", "tax_reserve", "earnings_vega",
        ],
        help="Select specific strategy playbook",
    )
    parser.add_argument("--persona", default="default", choices=["default", "traderlady"], help="Select AI voice & response persona")
    parser.add_argument("--live", action="store_true", help="Enable live Webull OpenAPI execution mode")
    parser.add_argument("--non-interactive", action="store_true", help="Run without human confirmation prompts")
    parser.add_argument("--interval", type=float, default=15.0, help="Monitor poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run single monitor evaluation sweep and exit")
    parser.add_argument("--license-key", default=None, help="Validate Whop commercial license key")
    parser.add_argument(
        "--arm", nargs=3, metavar=("SYMBOL", "LEVEL", "DIRECTION"),
        help="alerts: arm one, e.g. --arm SPY flip below (LEVEL may be a number or flip/pin/wall_above/wall_below)",
    )
    parser.add_argument("--disarm", default=None, metavar="ID", help="alerts: remove an alert by id")
    parser.add_argument("--note", default=None, help="alerts: optional note attached to an armed alert")
    parser.add_argument("--verify", action="store_true", help="audit: verify the hash chain's integrity")
    parser.add_argument("--reason", default=None, help="halt: optional reason recorded in the halt state")
    parser.add_argument("--mark", action="store_true", help="paper: mark open positions to market before printing the ledger")

    args = parser.parse_args()

    if args.license_key:
        from vesper.whop import WhopClient
        with WhopClient() as whop:
            val_res = whop.validate_license(args.license_key)
            if val_res.get("valid"):
                print(f"✅ Whop License Validated: {val_res.get('email', 'Active Member')}")
            else:
                print(f"❌ Whop License Validation Failed: {val_res.get('reason')}")
        sys.exit(0 if val_res.get("valid") else 1)

    if args.command == "halt":
        from core.halt import halt
        res = halt(reason=args.reason, source="cli") if args.reason else halt(source="cli")
        print(f"\n🛑 {res['message']}")
        sys.exit(0)

    if args.command == "resume":
        from core.halt import resume
        res = resume(source="cli")
        print(f"\n✅ {res['message']}")
        sys.exit(0)

    if args.command == "status":
        from core.halt import get_halt_status
        from core.paper_ledger import get_paper_summary
        from vesper.metrics import read_snapshot, bucket_approval_ages
        hs = get_halt_status()
        ps = get_paper_summary()
        print("\n" + "=" * 60)
        print("⚡ VESPER SYSTEM STATUS TELEMETRY")
        print("=" * 60)
        print(f"Emergency Halt: {'🛑 HALTED' if hs['is_halted'] else '✅ ACTIVE'}")
        if hs['is_halted']:
            print(f"  Reason: {hs['details'].get('reason')}")
            print(f"  Halted At: {hs['details'].get('halted_at')}")
            print(f"  Halted By: {hs['details'].get('halted_by')}")
        print("\n📊 Paper Ledger:")
        print(f"  Total NLV:      ${ps['total_nlv']:,.2f} ({ps['total_return_pct']:+.2f}%)")
        print(f"  Cash Balance:   ${ps['cash']:,.2f}")
        print(f"  Realized PnL:   ${ps['realized_pnl']:+,.2f}")
        print(f"  Unrealized PnL: ${ps['unrealized_pnl']:+,.2f}")
        print(f"  Open Positions: {ps['open_positions_count']}")
        print(f"  Closed Trades:  {ps['closed_trades_count']} (Win Rate: {ps['win_rate_pct']:.1f}%)")

        # Health/observability metrics -- written by a separately-running
        # `vesper loop` process on its own poll cadence (see vesper/loop.py),
        # not by this one-shot process. Report-only, and deliberately never
        # claims liveness: this is "as of" whenever that process last wrote
        # it, which may be minutes ago, or may not exist at all if `vesper
        # loop` was never started this session.
        snap = read_snapshot()
        print("\n📈 Health Metrics:")
        if snap is None:
            print("  (none yet -- start `vesper loop` to begin collecting)")
        else:
            print(f"  As of: {snap.get('generated_at', '?')} (from the last-running `vesper loop`, may be stale)")
            for bucket, endpoints in snap.get("broker_calls", {}).items():
                for endpoint, e in endpoints.items():
                    print(
                        f"  Broker[{bucket}] {endpoint}: ok={e['ok']} error={e['error']} "
                        f"rate_limited={e['rate_limited']} p50={e['p50_ms']}ms p95={e['p95_ms']}ms"
                    )
            for tier, e in snap.get("llm_calls", {}).items():
                counts = ", ".join(f"{k}={v}" for k, v in e.items() if k not in ("p50_ms", "p95_ms", "count"))
                print(f"  LLM[{tier}]: {counts} p50={e['p50_ms']}ms p95={e['p95_ms']}ms")
            for node, e in snap.get("tool_rejections", {}).items():
                print(f"  RiskGate[{node}]: passed={e['passed']} rejected={e['rejected']}")
            for mode, brokers in snap.get("order_outcomes", {}).items():
                for broker, statuses in brokers.items():
                    counts = ", ".join(f"{k}={v}" for k, v in statuses.items())
                    print(f"  Orders[{mode}/{broker}]: {counts}")
            qsnap = snap.get("quote_snapshot", {})
            if qsnap.get("sources"):
                print(f"  Quotes: sources={qsnap['sources']} max_age_sec={qsnap.get('max_age_sec')}")

        # Pending-approval age -- report-only labeling, see metrics.py's
        # bucket_approval_ages() docstring for why "stale" here is a label,
        # not an expiry. Read directly from this process (ApprovalRegistry is
        # disk-backed), not from the loop-written snapshot above.
        try:
            from vesper.bot.inbound import approval_registry
            pending = approval_registry.list_pending()
            if pending:
                ages = bucket_approval_ages([p.get("registered_at") for p in pending])
                print(f"\n⏳ Pending Approvals ({len(pending)}): {ages}")
        except Exception as e:
            print(f"\n⏳ Pending Approvals: unavailable ({e})")
        print("=" * 60)
        sys.exit(0)

    if args.command == "paper":
        from core.paper_ledger import get_paper_summary, get_paper_positions, mark_to_market
        if args.mark:
            asyncio.run(mark_to_market())
        ps = get_paper_summary()
        positions = get_paper_positions()
        print("\n" + "=" * 60)
        print("📜 VESPER PAPER TRADING LEDGER")
        print("=" * 60)
        print(f"Account NLV:    ${ps['total_nlv']:,.2f} ({ps['total_return_pct']:+.2f}%)")
        print(f"Cash:           ${ps['cash']:,.2f}")
        print(f"Realized PnL:   ${ps['realized_pnl']:+,.2f}")
        print(f"Unrealized PnL: ${ps['unrealized_pnl']:+,.2f}")
        print(f"Win Rate:       {ps['win_rate_pct']:.1f}% ({ps['closed_trades_count']} closed trades)")
        print(f"\nOpen Positions ({len(positions)}):")
        for p in positions:
            print(
                f"  • {p['ticker']} ({p['side']} {p['quantity']}x @ ${p['filled_price']:.2f}) -> "
                f"Cur: ${p.get('current_price', p['filled_price']):.2f} | "
                f"PnL: ${p.get('unrealized_pnl', 0.0):+,.2f} ({p.get('unrealized_pnl_pct', 0.0):+.1f}%)"
            )
        print("=" * 60)
        sys.exit(0)

    if args.command == "alerts":
        # Control surface for the restored alert stack. Arming/listing/removing
        # happens here; the alerts are actually EVALUATED by the watcher thread
        # inside `vesper loop` (see vesper/alerts_runner.py) -- a one-shot CLI
        # process can't watch anything after it exits.
        import alerts as alerts_mod

        store = alerts_mod.AlertStore()

        if args.arm:
            try:
                level = args.arm[1]
                a = alerts_mod.make_alert(
                    symbol=args.arm[0],
                    level=level,
                    direction=args.arm[2],
                    note=args.note or "",
                )
                store.add(a)
                print(f"\n✅ Armed: {alerts_mod.describe(a)}")
                print("   (evaluated by the watcher in `vesper loop` — start that if it isn't running)")
            except alerts_mod.AlertError as e:
                print(f"\n❌ {e}")
                print(f"   level must be a number or one of: {', '.join(alerts_mod.DYNAMIC_LEVELS)}")
                sys.exit(1)
            sys.exit(0)

        if args.disarm:
            print("\n✅ Removed." if store.remove(args.disarm) else "\n❌ No alert with that id.")
            sys.exit(0)

        listed = store.list()
        print("\n" + "=" * 60)
        print(f"🔔 VESPER ALERTS ({len(listed)})")
        print("=" * 60)
        if not listed:
            print("  (none armed — use: vesper.py alerts --arm SPY flip below)")
        for a in listed:
            fired = a.get("trigger_count") or 0
            print(
                f"  [{a['id']}] {alerts_mod.describe(a)}"
                f"  state={a.get('state', '?')}" + (f"  fired={fired}x" if fired else "")
            )
        print("=" * 60)
        sys.exit(0)

    if args.command == "audit":
        # --verify is accepted but currently a no-op flag since `audit` only
        # has one mode today; keeping it registered leaves room for a future
        # `vesper audit --export`/`--tail` without a breaking CLI change,
        # matching this file's existing habit of pre-registering flags
        # per-command (--arm/--disarm above).
        from core.audit_chain import verify_chain
        result = verify_chain()
        print("\n" + "=" * 60)
        print("🔗 VESPER AUDIT CHAIN INTEGRITY")
        print("=" * 60)
        print(f"Entries: {result.get('entry_count', result.get('break_index', '?'))}")
        if result["valid"]:
            print("✅ Chain intact -- no tampering detected.")
        else:
            print(f"🛑 BROKEN at entry #{result['break_index']} (node={result.get('break_node')}, session={result.get('break_session')})")
            print(f"   Reason: {result['break_reason']}")
        print("=" * 60)
        sys.exit(0 if result["valid"] else 1)

    if args.command == "morning":
        from vesper.morning import generate_morning_plan
        asyncio.run(generate_morning_plan())
        sys.exit(0)

    if args.command == "monitor":
        from vesper.monitor import run_monitor_loop
        asyncio.run(run_monitor_loop(interval_sec=args.interval, live=args.live, once=args.once))
        sys.exit(0)

    if args.command == "listen":
        # Starts the Telegram long-polling loop and Discord gateway client concurrently
        # to feed ApprovalRegistry real Approve/Reject taps and /halt /resume commands.
        # Outbound-only -- no public ports opened.
        from vesper.graph import build_trading_graph
        from vesper.bot.inbound import approval_registry
        from vesper.bot.telegram_polling import run_telegram_polling_loop
        from vesper.bot.discord_gateway import run_discord_gateway_bot

        print("\n" + "=" * 60)
        print("📡 VESPER INBOUND APPROVAL LISTENER (Telegram & Discord)")
        print("=" * 60)

        async def _run_listeners() -> None:
            app = await build_trading_graph(checkpointer=True)
            approval_registry.set_graph_app(app)
            await asyncio.gather(
                run_telegram_polling_loop(),
                run_discord_gateway_bot(),
                return_exceptions=True,
            )

        try:
            asyncio.run(_run_listeners())
        except KeyboardInterrupt:
            print("\n[!] Vesper listener interrupted by user.")
        sys.exit(0)

    if args.command == "loop":
        # Unattended market-hours scheduling: scheduled full scans + the
        # continuous position monitor in the background. Run `vesper listen`
        # as a separate process alongside this for --live to actually be
        # able to approve anything it drafts -- this command never places an
        # order without an explicit remote approval tap (see vesper/loop.py).
        from vesper.loop import run_continuous_loop

        loop_mode = "live" if args.live else "dry_run"
        try:
            asyncio.run(run_continuous_loop(
                mode=loop_mode, playbook=args.playbook, persona=args.persona,
                monitor_interval_sec=args.interval,
            ))
        except KeyboardInterrupt:
            print("\n[!] Vesper continuous loop interrupted by user.")
        sys.exit(0)

    mode = "live" if args.live else "dry_run"
    playbook = args.playbook
    target_ticker = args.ticker

    if args.command == "0dte":
        playbook = "0dte_flow"
        target_ticker = "SPY"
    elif args.command == "analyze":
        if not target_ticker:
            target_ticker = "SPY"

    try:
        asyncio.run(
            run_agent_session(
                mode=mode,
                playbook=playbook,
                target_ticker=target_ticker,
                interactive=not args.non_interactive,
                persona=args.persona,
            )
        )
    except KeyboardInterrupt:
        print("\n[!] Vesper session interrupted by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
