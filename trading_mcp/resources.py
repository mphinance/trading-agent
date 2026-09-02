"""MCP Resources for Vesper skills and operating rules.

M10-04, M10-05, M10-06.
Surfaces skills as readable @mcp.resource at skill://<name>,
plus curated skill://rules with path-traversal safety and read-only protocol.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

# Curated rules resource content (M10-05)
# Covers:
# 1. "gamma marks positioning not a forecast"
# 2. NVDA-to-'in video' mis-transcription example
# 3. "voice may do anything that cannot increase exposure" (exposure rule)
# 4. "buttons move money"
RULES_CONTENT = """# Vesper Operating Rules for Voice Clients

1. Core Exposure Rule:
   Voice may do anything that cannot increase exposure. Reading state, checking gamma,
   arming alerts, tagging or snoozing setups, and emergency halt are permitted.
   Approve and resume are strictly forbidden from the voice interface.

2. Approvals & Execution:
   Buttons move money. Spoken transcripts are ambiguous ("approve" - which trade?).
   Interactive cards on Discord or Telegram remain the only way a trade proposal is executed.
   If a setup triggers, inform the operator: "Press it yourself, I cannot approve this."

3. Tape & Gamma Mechanics:
   Gamma marks positioning not a forecast. Dealer positioning reveals where market makers
   are forced to hedge, not where price is guaranteed to go. Respect the tape over narrative.

4. Audio & Transcription Defense:
   Voice models frequently mishear market tickers (for example, hearing "NVDA" as "in video").
   Always echo the resolved ticker and quantity aloud for explicit verbal confirmation.
   Ambiguous queries draft nothing.
"""


def read_skill_content(name: str) -> str:
    """Read on-disk SKILL.md for a given skill name with path traversal defense (M10-06)."""
    if not name:
        raise ValueError("Skill name cannot be empty")

    if name == "rules":
        return RULES_CONTENT

    # Strict path traversal check: reject slashes, backslashes, and dot-dot
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError(f"Path traversal rejected: '{name}' is not a valid skill name.")

    skill_file = (SKILLS_DIR / name / "SKILL.md").resolve()
    # Confirm it is inside SKILLS_DIR
    try:
        skill_file.relative_to(SKILLS_DIR.resolve())
    except ValueError:
        raise ValueError(f"Path traversal rejected: '{name}' escapes skills directory.")

    if not skill_file.is_file():
        raise FileNotFoundError(f"Skill '{name}' not found at {skill_file}")

    return skill_file.read_text(encoding="utf-8")


def discover_skills() -> list[str]:
    """Discover all skill names that have a SKILL.md file on disk."""
    if not SKILLS_DIR.is_dir():
        return []
    skills = []
    for skill_path in sorted(SKILLS_DIR.iterdir()):
        if skill_path.is_dir() and (skill_path / "SKILL.md").is_file():
            skills.append(skill_path.name)
    return skills


def register_skill_resources(mcp: Any) -> list[str]:
    """Register every skill under skills/ as skill://<name> plus skill://rules."""
    registered = []

    # 1. Register skill://rules (M10-05)
    @mcp.resource("skill://rules")
    def read_rules() -> str:
        """Curated voice client operating rules."""
        return RULES_CONTENT

    registered.append("skill://rules")

    # 2. Register every discovered skill directory dynamically (M10-04)
    skills = discover_skills()
    for skill_name in skills:
        uri = f"skill://{skill_name}"

        def _make_reader(s_name: str) -> Callable[[], str]:
            def _reader() -> str:
                return read_skill_content(s_name)
            _reader.__name__ = f"skill_{s_name.replace('-', '_')}"
            _reader.__doc__ = f"Documentation and instructions for {s_name}"
            return _reader

        mcp.resource(uri)(_make_reader(skill_name))
        registered.append(uri)

    logger.info("Registered %d skill resources (including skill://rules)", len(registered))
    return registered
