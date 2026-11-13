"""The ``trading-agent`` command: setup, undo, catalog build, scan.

``main`` never lets a traceback reach the user. Ctrl-C, end of input and a closed pipe each print one plain
line; any other failure prints one plain line with the exception TYPE only (its text could hold a secret).
Exit codes: 0 done, 1 error or stopped, 2 usage or not available in this build, 130 Ctrl-C.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from trading_agent import paths

STOPPED_NEUTRAL = "Stopped."
STOPPED = "Setup stopped. Run `trading-agent setup` again to continue where you left off."
NOT_IN_BUILD = "The setup files for the agent are not in this build yet."
NOT_A_DISTRIBUTION = "That folder does not look like a Hermes distribution: it has no distribution.yaml."
UNEXPECTED = ("Something unexpected went wrong ({kind}). Run the command again; "
              "`trading-agent undo` removes anything setup created.")
NO_RECEIPT_ANY = "I found no setup receipt, so there is nothing to undo."
NO_RECEIPT_ONE = "I found no setup receipt for {name}, so there is nothing to undo."
BAD_NAME = "That is not a valid profile name."
BAD_RECEIPT = "The setup receipt for {name} could not be read, so I removed nothing."
WHICH = "More than one setup receipt exists. Which profile should I undo?"
UNDO_PLAN = ("This will remove the Hermes profile {name} that setup created.{env}\n"
             "Nothing outside that profile is touched.")
UNDO_ENV = " That includes these names in its private .env file: {names}."
UNDO_CONFIRM = "Remove it?"
UNDO_NO = "Nothing was removed."
UNDO_DONE = "Removed the profile {name} and its setup receipt."
UNDO_NOTHING = ("I did not remove anything: this profile is not one this tool created, "
                "or it is already gone.")
UNDO_FAILED = "I could not remove everything: {report}"


# ---------------------------------------------------------------- subcommands
# Heavy modules are imported inside the commands, so a Ctrl-C during startup is still caught by ``main``.

def _platform():
    from trading_agent.platform import LinuxPlatform
    return LinuxPlatform()


def _detectors(platform):
    from trading_agent.wizard.flow import default_detectors
    return default_detectors(platform)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _valid_name(name: str) -> bool:
    from trading_agent.profile.hermes_cli import validate_profile_name
    try:
        validate_profile_name(name)
    except ValueError:
        return False
    return True


def _cmd_setup(args: argparse.Namespace) -> int:
    from trading_agent.catalog.snapshot import load_catalog
    from trading_agent.libraries import load_libraries
    from trading_agent.profile.hermes_cli import HermesCli
    from trading_agent.secrets import choose_store
    from trading_agent.wizard.flow import Deps, run_wizard
    from trading_agent.wizard.io import ConsoleIO
    if args.profile is not None and not _valid_name(args.profile):
        _err(BAD_NAME)
        return 2
    if args.distribution:
        dist = Path(args.distribution)
        if not (dist.is_dir() and (dist / "distribution.yaml").is_file()):
            _err(NOT_A_DISTRIBUTION)
            return 2
    else:
        dist = paths.distribution_dir()
        if dist is None or not (dist / "distribution.yaml").is_file():
            _err(NOT_IN_BUILD)
            return 2
    platform = _platform()
    deps = Deps(
        platform=platform, hermes=HermesCli(platform), detectors=_detectors(platform),
        catalog=load_catalog(), libraries=load_libraries(),
        store_factory=lambda profile_dir: choose_store(platform, profile_dir), source_dir=dist,
        profile_name=args.profile)
    receipt = run_wizard(ConsoleIO(), deps)
    return 0 if receipt is not None else 1


def _pick_profile(args: argparse.Namespace, platform, io) -> str | None:
    from trading_agent.profile.receipt import Receipt
    from trading_agent.wizard.io import Option
    if args.profile:
        return args.profile
    names = [n for n in Receipt.list_receipts(platform) if _valid_name(n)]
    if not names:
        _err(NO_RECEIPT_ANY)
        return None
    if len(names) == 1:
        return names[0]
    return io.choose(WHICH, [Option(n, n) for n in names])


def _cmd_undo(args: argparse.Namespace) -> int:
    from trading_agent.profile.hermes_cli import HermesCli
    from trading_agent.profile.receipt import Receipt, ReceiptError
    from trading_agent.profile.writer import WriteError, rollback
    from trading_agent.secrets import choose_store
    from trading_agent.wizard.io import ConsoleIO
    platform = _platform()
    io = ConsoleIO()
    if args.profile and not _valid_name(args.profile):
        _err(BAD_NAME)
        return 2
    name = _pick_profile(args, platform, io)
    if name is None:
        return 1
    try:
        if Receipt.path(platform, name).is_symlink():
            raise ReceiptError("symlink")       # a link could point at another profile's receipt
        receipt = Receipt.load(platform, name)
    except (ReceiptError, OSError):
        _err(BAD_RECEIPT.format(name=name))
        return 1
    if receipt is None:
        _err(NO_RECEIPT_ONE.format(name=name))
        return 1
    # What is confirmed must be what is removed: the file's own claim has to match its name and be valid.
    if receipt.profile_name != name or not _valid_name(receipt.profile_name):
        _err(BAD_RECEIPT.format(name=name))
        return 1
    env = UNDO_ENV.format(names=", ".join(_safe(n) for n in receipt.env_names_written)) \
        if receipt.env_names_written else ""
    print(UNDO_PLAN.format(name=name, env=env))
    if not io.confirm(UNDO_CONFIRM, default=False):
        print(UNDO_NO)
        return 0
    try:
        acted = rollback(receipt, hermes=HermesCli(platform),
                         store_factory=lambda profile_dir: choose_store(platform, profile_dir))
    except WriteError as exc:
        _err(UNDO_FAILED.format(report=_safe(exc.report)))
        return 1
    if acted:
        print(UNDO_DONE.format(name=name))
        return 0
    _err(UNDO_NOTHING)
    return 1


def _cmd_catalog_build(args: argparse.Namespace) -> int:
    from trading_agent.catalog import generate
    argv = ["--catalog-root", str(args.catalog_root), "--out", str(args.out)]
    if args.overlay:
        argv += ["--overlay", str(args.overlay)]
    return generate.main(argv)


def _safe(text: object) -> str:
    """Printable ASCII, credential shapes masked, capped: a scan line carries names, never anything value-like."""
    cleaned = "".join(c if " " <= c <= "~" else "?" for c in str(text))
    from trading_agent.secrets import redact_patterns
    return redact_patterns(cleaned)[:120]


def _cmd_scan(args: argparse.Namespace) -> int:
    platform = _platform()
    det = _detectors(platform)
    findings = [*det.env(), *det.mcp_files(True), *det.ai_clis(), *det.ports()]
    rows = [{"id": _safe(f.id), "kind": f.kind, "state": f.state, "names": [_safe(n) for n in f.names]}
            for f in findings]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['id']} | {r['kind']} | {r['state']} | {', '.join(r['names'])}")
    return 0


# ---------------------------------------------------------------- parser

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="trading-agent",
        description="Set up a Hermes trading agent in its own new profile. Nothing is required and "
                    "nothing outside that profile is changed.")
    sub = ap.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("setup", help="run the setup wizard", description="Run the setup wizard.")
    p.add_argument("--profile", help="name for the new profile (default: the wizard asks, suggesting trader)")
    p.add_argument("--distribution", metavar="DIR", help="use this distribution directory (development)")
    p.set_defaults(func=_cmd_setup)

    p = sub.add_parser("undo", help="remove what setup created",
                       description="Remove the profile setup created, using its saved receipt.")
    p.add_argument("--profile", help="profile to remove (default: the only one with a receipt)")
    p.set_defaults(func=_cmd_undo)

    p = sub.add_parser("catalog", help="catalog tools", description="Catalog tools.")
    csub = p.add_subparsers(dest="catalog_command", required=True, metavar="ACTION")
    b = csub.add_parser("build", help="build the catalog snapshot from a catalog checkout",
                        description="Build the catalog snapshot JSON from a catalog checkout.")
    b.add_argument("--catalog-root", required=True, type=Path, help="the catalog checkout")
    b.add_argument("--out", required=True, type=Path, help="file to write")
    b.add_argument("--overlay", type=Path, default=None, help="overlay JSON (default: the packaged one)")
    b.set_defaults(func=_cmd_catalog_build)

    p = sub.add_parser("scan", help="list what setup could find (names only)",
                       description="List what setup could find on this machine. Prints names only, never values.")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=_cmd_scan)
    return ap


def _quiet_stdout() -> None:
    """After a closed pipe, point stdout at /dev/null so the interpreter's exit flush stays silent."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    command: str | None = None
    try:
        try:
            args = _parser().parse_args(argv)
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
        command = args.command
        return args.func(args)
    except KeyboardInterrupt:
        print(STOPPED if command in (None, "setup") else STOPPED_NEUTRAL, file=sys.stderr)
        return 130
    except EOFError:
        print(STOPPED if command in (None, "setup") else STOPPED_NEUTRAL, file=sys.stderr)
        return 1
    except BrokenPipeError:
        _quiet_stdout()
        return 1
    except Exception as exc:  # noqa: BLE001 - the type name only: str(exc) could hold a secret
        print(UNEXPECTED.format(kind=type(exc).__name__), file=sys.stderr)
        return 1
