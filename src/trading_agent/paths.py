"""Where the packaged files live."""
from __future__ import annotations

import atexit
import shutil
import tempfile
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path


# Extracted throwaway copies made by this process. Hermes records the folder it installs from as the profile's
# `source:`, and `hermes profile update` reads it back, so a folder listed here is gone by then.
_TEMP_COPIES: set[Path] = set()


def _packaged_root() -> Traversable:
    """Where the wheel puts the distribution (hatch force-include)."""
    return resources.files("trading_agent").joinpath("data").joinpath("distribution")


def _checkout_root() -> Path:
    """The repo root of a source checkout, where the distribution files are the source of truth."""
    return Path(__file__).resolve().parents[2]


def _has_manifest(root: Traversable) -> bool:
    try:
        return root.joinpath("distribution.yaml").is_file()
    except OSError:
        return False


def _distribution_root() -> Traversable:
    """The packaged files when present, else the checkout root (editable or `uv run`), else the packaged path."""
    packaged = _packaged_root()
    if _has_manifest(packaged):
        return packaged
    checkout = _checkout_root()
    return checkout if _has_manifest(checkout) else packaged


def _copy_tree(src: Traversable, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dest / child.name
        if child.is_dir():
            _copy_tree(child, target)
        else:
            target.write_bytes(child.read_bytes())


def distribution_dir() -> Path | None:
    """The packaged Hermes distribution directory, or None when this build does not include it.

    A normal install (wheel unpacked into site-packages) and a checkout return the real folder itself, so the
    `source:` Hermes records stays valid and `uv tool upgrade` refreshes it. Only a zipped install is extracted
    once into a temp directory that is removed at exit; see ``source_is_durable``."""
    root = _distribution_root()
    try:
        if not root.is_dir():
            return None
    except OSError:
        return None
    if isinstance(root, Path):
        return root
    tmp = Path(tempfile.mkdtemp(prefix="trading-agent-dist-"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)   # runs on normal exit, an exception and Ctrl-C
    _TEMP_COPIES.add(tmp)
    _copy_tree(root, tmp)
    return tmp


def source_is_durable(dist: Path) -> bool:
    """False for a throwaway extracted copy: Hermes would record a folder that is gone after this process."""
    return Path(dist) not in _TEMP_COPIES
