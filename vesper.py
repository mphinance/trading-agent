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
        """
    )
    
    parser.add_argument("command", nargs="?", default="scan", choices=["scan", "analyze", "0dte", "morning"], help="Action command")
    parser.add_argument("ticker", nargs="?", default=None, help="Target symbol for analysis")
    parser.add_argument("--playbook", default="all", choices=["all", "momentum_squeeze", "0dte_flow", "institutional_convergence"], help="Select specific strategy playbook")
    parser.add_argument("--persona", default="default", choices=["default", "traderlady"], help="Select AI voice & response persona")
    parser.add_argument("--live", action="store_true", help="Enable live Webull OpenAPI execution mode")
    parser.add_argument("--non-interactive", action="store_true", help="Run without human confirmation prompts")

    args = parser.parse_args()

    if args.command == "morning":
        from vesper.morning import generate_morning_plan
        asyncio.run(generate_morning_plan())
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
            )
        )
    except KeyboardInterrupt:
        print("\n[!] Vesper session interrupted by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
