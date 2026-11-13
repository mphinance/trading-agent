#!/usr/bin/env python3
"""Render the docs transcripts and wizard screen SVGs from a real run of the CLI.

Runs ``trading-agent`` in-process against a temporary HERMES_HOME, HOME and XDG config dir, with the
shim ``hermes`` from ``tests/fixtures`` on PATH, so nothing real is read or written. The wizard is
driven by scripted answers. Output is deterministic: temp paths become ``<home>``, there are no
timestamps or ids, and every secret is shown as ``[hidden]``.

    python scripts/render_transcript.py            # write docs/transcripts/*.txt and docs/assets/wizard-*.svg
    python scripts/render_transcript.py --check    # exit 1 if the committed files differ from a fresh run

Standard library only. ``main(argv=None) -> int``.
"""
from __future__ import annotations

import argparse
import builtins
import os
import shutil
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "tests" / "fixtures" / "hermes_shim.py"
TRANSCRIPT_DIR = ROOT / "docs" / "transcripts"
ASSET_DIR = ROOT / "docs" / "assets"

# Fake values. Neither is a real key. Every 4-character window holds a symbol, so a window cannot
# match ordinary wizard text by chance (the docs test sweeps all output for these windows).
FAKE_KEY_ID = "pK!7x%Q2^m*Z9+c=L4~w@R8"
FAKE_SECRET_KEY = "Hv#3d&B6$n!Y1%t^F5*u@E0"
FAKE_ENV_KEY = "xN!4c%J8^k*P2+v=T6~b@D1"
SECRETS = (FAKE_KEY_ID, FAKE_SECRET_KEY, FAKE_ENV_KEY)

MAX_TRANSCRIPT_LINES = 140
SVG_COLS = 92          # wrap width of an SVG screen, in characters
SVG_MAX_LINES = 34

HIDDEN = "[hidden]"


class Recorder:
    """Stands in for stdout and stdin. Records output in chunks; a chunk ends at every prompt answer."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.chunks: list[str] = []
        self.answers: list[tuple[str, bool]] = []     # (typed text, is_secret)
        self.consumed: list[str] = []

    # stdout side
    def write(self, text: str) -> int:
        self.parts.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def close_chunk(self) -> None:
        self.chunks.append("".join(self.parts))
        self.parts = []

    def text(self) -> str:
        return "".join(self.chunks) + "".join(self.parts)

    # stdin side (only the secret prompt reads stdin; plain prompts go through the patched input())
    def _take(self, want_secret: bool) -> str:
        if not self.answers:
            raise EOFError
        value, is_secret = self.answers.pop(0)
        if is_secret != want_secret:
            raise AssertionError("scripted answer kind does not match the prompt")
        return value

    def readline(self, *_args) -> str:
        value = self._take(True)
        self.parts.append(HIDDEN + "\n")
        self.close_chunk()
        return value + "\n"


class Secret(str):
    """A scripted answer that must never be printed."""


def _normalise(text: str, roots: list[str]) -> str:
    for r in sorted(set(roots), key=len, reverse=True):
        text = text.replace(r, "<home>")
    return "\n".join(line.rstrip() for line in text.split("\n"))


class World:
    """A throwaway home with the shim hermes on PATH. Everything lives under one temp directory."""

    def __init__(self, base: Path) -> None:
        self.root = base.resolve()
        self.home = self.root / "home"
        self.config = self.root / "config"
        self.hermes_home = self.root / "hermes"
        self.dist = self.root / "dist"
        self.bin = self.root / "bin"
        self.ctl = self.root / "ctl"
        for d in (self.home, self.config, self.hermes_home / "profiles", self.dist, self.bin, self.ctl):
            d.mkdir(parents=True, exist_ok=True)
        for name in ("distribution.yaml", "SOUL.md", "config.yaml"):
            shutil.copy(ROOT / name, self.dist / name)
        exe = self.bin / "hermes"
        exe.write_text(
            f"#!{sys.executable}\nimport os, runpy, sys\n"
            f"os.environ['SHIM_LOG'] = {str(self.ctl / 'calls.jsonl')!r}\n"
            f"os.environ['SHIM_CONTROL'] = {str(self.ctl / 'control.json')!r}\n"
            f"sys.argv[0] = {str(SHIM)!r}\nrunpy.run_path({str(SHIM)!r}, run_name='__main__')\n")
        exe.chmod(0o755)
        self.environ = {
            "PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.config),
            "HERMES_HOME": str(self.hermes_home), "OPENROUTER_API_KEY": FAKE_ENV_KEY,
        }

    def roots(self) -> list[str]:
        # The resolved root first; also the unresolved spelling in case a path was printed that way.
        return [str(self.root)]

    def run(self, argv: list[str], answers: list, *, show: list[str] | None = None) -> tuple[int, str, list[str]]:
        """Run one CLI command. Returns (exit code, transcript text, per-prompt chunks)."""
        from trading_agent import cli

        rec = Recorder()
        rec.answers = [(str(a), isinstance(a, Secret)) for a in answers]

        def fake_input(prompt: str = "") -> str:
            rec.write(prompt)
            if not rec.answers:
                raise EOFError
            value, is_secret = rec.answers.pop(0)
            if is_secret:
                raise AssertionError("a secret answer reached a plain prompt")
            rec.write(value + "\n")
            rec.close_chunk()
            return value

        from trading_agent.platform import LinuxPlatform
        from trading_agent.wizard.flow import Detectors, default_detectors
        from trading_agent.detect.ports import probe_ports

        bin_dir = self.bin

        class _Platform(LinuxPlatform):
            def which(self, name: str):          # only the shim dir: this machine's own CLIs never show up
                found = shutil.which(name, path=str(bin_dir))
                return Path(found) if found else None

        def _detectors(platform):
            real = default_detectors(platform)
            return Detectors(env=real.env, mcp_files=real.mcp_files, ai_clis=real.ai_clis,
                             ports=lambda: probe_ports(connect=lambda host, port, timeout: False))

        saved_cli = (cli._platform, cli._detectors)
        cli._platform, cli._detectors = _Platform, _detectors
        saved_environ = os.environ
        saved = (sys.stdout, sys.stderr, sys.stdin, builtins.input)
        os.environ = dict(self.environ)           # type: ignore[assignment]
        sys.stdout = sys.stderr = rec             # type: ignore[assignment]
        sys.stdin = rec                            # type: ignore[assignment]
        builtins.input = fake_input
        try:
            rc = cli.main(argv)
        finally:
            sys.stdout, sys.stderr, sys.stdin, builtins.input = saved
            os.environ = saved_environ
            cli._platform, cli._detectors = saved_cli
        if rec.answers:
            raise AssertionError(f"unused scripted answers for {argv}: {len(rec.answers)}")
        roots = self.roots()
        body = _normalise(rec.text(), roots)
        shown = " ".join(show if show is not None else argv)
        text = f"$ trading-agent {_normalise(shown, roots)}\n{body}".rstrip("\n") + "\n"
        chunks = [_normalise(c, roots) for c in rec.chunks]
        tail = _normalise("".join(rec.parts), roots)
        if tail.strip():
            chunks.append(tail)
        return rc, text, chunks


# Scripted answers for the setup happy path, typed as a person would (menu numbers).
SETUP_ANSWERS = [
    "1",                     # Start
    "",                      # profile name: Enter for the suggested "trader"
    "1",                     # Look around
    "6",                     # AI brain: Skip for now
    "1",                     # Continue (nothing to test yet)
    "3",                     # Broker: Alpaca
    "1",                     # Route: local
    "1",                     # Continue
    "1", Secret(FAKE_KEY_ID),      # Paste ALPACA_API_KEY
    "1", Secret(FAKE_SECRET_KEY),  # Paste ALPACA_SECRET_KEY
    "true",                  # ALPACA_PAPER_TRADE
    "3",                     # Skill libraries: Done
    "1",                     # Do it
]


def _screens(chunks: list[str]) -> dict[str, str]:
    """Pick the four key screens out of the setup run by what they say."""
    pick = {
        "first-screen": lambda c: c.startswith("Set up your trading agent."),
        "found": lambda c: "Found on this computer" in c,
        "review": lambda c: "Ready. Here is exactly what I will do." in c,
        "done": lambda c: "You are set up." in c,
    }
    out: dict[str, str] = {}
    for key, test in pick.items():
        hits = [c for c in chunks if test(c)]
        if not hits:
            raise RuntimeError(f"screen {key!r} not found in the setup run")
        out[key] = hits[0]
    return out


def render_all() -> dict[Path, str]:
    """Run every command once and return {path: file text}. Writes nothing."""
    files: dict[Path, str] = {}
    with tempfile.TemporaryDirectory(prefix="ta-docs-") as tmp:
        w = World(Path(tmp))
        bundle = w.root / "trader.bundle"
        rc, scan_text, _ = w.run(["scan"], [])
        _expect(rc == 0, "scan")
        rc, setup_text, chunks = w.run(["setup", "--distribution", str(w.dist)], SETUP_ANSWERS,
                                       show=["setup"])
        _expect(rc == 0, "setup")
        rc, export_text, _ = w.run(["export", "--profile", "trader", "-o", str(bundle)], [],
                                   show=["export", "--profile", "trader", "-o", "trader.bundle"])
        _expect(rc == 0, "export")
        rc, import_text, _ = w.run(
            ["import", str(bundle), "--name", "trader2"],
            IMPORT_ANSWERS, show=["import", "trader.bundle", "--name", "trader2"])
        _expect(rc == 0, "import")
        rc, undo_text, _ = w.run(["undo", "--profile", "trader2"], ["y"], show=["undo", "--profile", "trader2"])
        _expect(rc == 0, "undo")
        for name, text in (("scan", scan_text), ("setup", setup_text), ("export", export_text),
                           ("import", import_text), ("undo", undo_text)):
            files[TRANSCRIPT_DIR / f"{name}.txt"] = text
        screens = _screens(chunks)
        titles = {
            "first-screen": ("The first screen of trading-agent setup",
                             "A dark terminal window showing the wizard's opening message and a two-item menu: "
                             "Start, or What does this do. The answer typed is 1."),
            "found": ("What setup found on this computer",
                      "A dark terminal window listing one key found by name only, an OpenRouter API key in "
                      "an environment variable, then a menu for picking where the agent's brain comes from. "
                      "No key value is shown."),
            "review": ("The review screen before anything is written",
                       "A dark terminal window listing exactly what setup will do: create one profile, "
                       "install the Alpaca broker connection, and write three names into the profile's "
                       "private key file. Nothing else is changed."),
            "done": ("Setup finished",
                     "A dark terminal window ending in You are set up, with the broker choice, who can read "
                     "each key, what is left to finish later, and the undo command."),
        }
        for key, chunk in screens.items():
            title, desc = titles[key]
            files[ASSET_DIR / f"wizard-{key}.svg"] = svg_window(title, desc, chunk)
    for path, text in files.items():
        if path.suffix == ".txt":
            n = len(text.splitlines())
            _expect(n <= MAX_TRANSCRIPT_LINES, f"{path.name} has {n} lines (cap {MAX_TRANSCRIPT_LINES})")
    return files


IMPORT_ANSWERS = [
    "y",                                   # Import it as a new profile? (the default is No)
    Secret(FAKE_KEY_ID),                   # ALPACA_API_KEY
    Secret(""),                            # ALPACA_PAPER_TRADE: Enter to skip
    Secret(FAKE_SECRET_KEY),               # ALPACA_SECRET_KEY
]


def _expect(ok: bool, what: str) -> None:
    if not ok:
        raise RuntimeError(f"transcript run failed: {what}")


# ----------------------------------------------------------------------------------------- SVG

def _wrap(line: str, cols: int) -> list[str]:
    if len(line) <= cols:
        return [line]
    indent = " " * (len(line) - len(line.lstrip()) + 2)
    out, cur = [], line
    while len(cur) > cols:
        cut = cur.rfind(" ", 0, cols)
        if cut <= len(indent):
            cut = cols
        out.append(cur[:cut].rstrip())
        cur = indent + cur[cut:].lstrip()
    out.append(cur)
    return out


def svg_window(title: str, desc: str, screen: str) -> str:
    lines: list[str] = []
    for raw in screen.rstrip("\n").split("\n"):
        lines.extend(_wrap(raw, SVG_COLS))
    if len(lines) > SVG_MAX_LINES:
        keep = SVG_MAX_LINES - 1
        lines = lines[:keep] + ["..."]
    char_w, line_h, pad_x, top = 8, 19, 18, 52
    width = SVG_COLS * char_w + pad_x * 2
    height = top + line_h * len(lines) + 20
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">',
        f"  <title id=\"t\">{escape(title)}</title>",
        f"  <desc id=\"d\">{escape(desc)}</desc>",
        f'  <rect x="0" y="0" width="{width}" height="{height}" rx="10" fill="#1e1f26"/>',
        f'  <rect x="0" y="0" width="{width}" height="34" rx="10" fill="#2b2d37"/>',
        f'  <rect x="0" y="24" width="{width}" height="10" fill="#2b2d37"/>',
        '  <circle cx="20" cy="17" r="6" fill="#ff5f56"/>',
        '  <circle cx="40" cy="17" r="6" fill="#ffbd2e"/>',
        '  <circle cx="60" cy="17" r="6" fill="#27c93f"/>',
        f'  <text x="{width // 2}" y="22" fill="#9aa0b4" font-family="monospace" font-size="12" '
        f'text-anchor="middle">trading-agent</text>',
        '  <g font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" font-size="13" '
        'fill="#d7dae0" xml:space="preserve">',
    ]
    for i, line in enumerate(lines):
        y = top + i * line_h
        colour = ' fill="#7ee787"' if line.startswith("> ") or line.startswith("$ ") else ""
        out.append(f'    <text x="{pad_x}" y="{y}"{colour} xml:space="preserve">{escape(line)}</text>')
    out += ["  </g>", "</svg>", ""]
    return "\n".join(out)


# ----------------------------------------------------------------------------------------- main

def check(files: dict[Path, str]) -> list[Path]:
    """Paths whose committed text differs from a fresh render (or that are missing)."""
    bad = []
    for path, text in files.items():
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            bad.append(path)
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Regenerate docs/transcripts/*.txt and docs/assets/wizard-*.svg.")
    ap.add_argument("--check", action="store_true", help="do not write; exit 1 if any file differs")
    args = ap.parse_args(argv)
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    files = render_all()
    if args.check:
        bad = check(files)
        for p in bad:
            print(f"differs or missing: {p.relative_to(ROOT)}", file=sys.stderr)
        return 1 if bad else 0
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
