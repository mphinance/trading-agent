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
        """
    )
    
    parser.add_argument(
        "command",
        nargs="?",
        default="scan",
        choices=["scan", "analyze", "0dte", "morning", "monitor", "halt", "resume", "status", "paper", "listen"],
        help="Action command",
    )
    parser.add_argument("ticker", nargs="?", default=None, help="Target symbol for analysis")
    parser.add_argument("--playbook", default="all", choices=["all", "momentum_squeeze", "0dte_flow", "institutional_convergence"], help="Select specific strategy playbook")
    parser.add_argument("--persona", default="default", choices=["default", "traderlady"], help="Select AI voice & response persona")
    parser.add_argument("--live", action="store_true", help="Enable live Webull OpenAPI execution mode")
    parser.add_argument("--non-interactive", action="store_true", help="Run without human confirmation prompts")
    parser.add_argument("--interval", type=float, default=15.0, help="Monitor poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run single monitor evaluation sweep and exit")
    parser.add_argument("--license-key", default=None, help="Validate Whop commercial license key")

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
        from vesper.halt import halt
        res = halt(reason=args.reason, source="cli")
        print(f"\n🛑 {res['message']}")
        sys.exit(0)

    if args.command == "resume":
        from vesper.halt import resume
        res = resume(source="cli")
        print(f"\n✅ {res['message']}")
        sys.exit(0)

    if args.command == "status":
        from vesper.halt import get_halt_status
        from vesper.paper_ledger import get_paper_summary
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
        print("=" * 60)
        sys.exit(0)

    if args.command == "paper":
        from vesper.paper_ledger import get_paper_summary, get_paper_positions, mark_to_market
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

        app = build_trading_graph(checkpointer=True)
        approval_registry.set_graph_app(app)
        print("\n" + "=" * 60)
        print("📡 VESPER INBOUND APPROVAL LISTENER (Telegram & Discord)")
        print("=" * 60)

        async def _run_listeners() -> None:
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
