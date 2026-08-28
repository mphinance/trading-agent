"""Autonomous Skill Creation & Evolution Engine (Synthesized from Hermes Agent)."""

from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


def create_new_skill(
    name: str,
    description: str,
    content_markdown: str,
    references: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Autonomously create a new standardized Agent Skill.
    
    Args:
        name: Skill folder and YAML name (e.g., 'gap-and-go', 'fomc-drift')
        description: Brief trigger description for when an agent should use it
        content_markdown: Full instruction markdown
        references: Optional dict of filename -> content to place in references/
    """
    skill_dir = SKILLS_DIR / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    
    skill_md = skill_dir / "SKILL.md"
    
    # Formulate YAML frontmatter conforming to Agent Skills standard
    full_content = f"""---
name: {name}
description: {description}
---

{content_markdown.strip()}
"""
    skill_md.write_text(full_content)
    
    if references:
        ref_dir = skill_dir / "references"
        ref_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in references.items():
            (ref_dir / filename).write_text(content)
            
    logger.info(f"Autonomously created Agent Skill: {name} at {skill_dir}")
    return {
        "status": "success",
        "skill_name": name,
        "path": str(skill_md),
        "message": f"Successfully created skill '{name}' and registered in {SKILLS_DIR}",
    }


def evolve_skill(
    name: str,
    new_findings: str,
) -> Dict[str, Any]:
    """Append post-trade lessons and parameter calibration to a skill's references."""
    skill_dir = SKILLS_DIR / name
    if not skill_dir.exists():
        return {"status": "error", "message": f"Skill '{name}' not found."}
        
    lessons_file = skill_dir / "references" / "learned_lessons.md"
    lessons_file.parent.mkdir(parents=True, exist_ok=True)
    
    with open(lessons_file, "a") as f:
        f.write(f"\n\n### Reflection Update\n{new_findings.strip()}\n")
        
    return {
        "status": "success",
        "skill_name": name,
        "message": f"Appended learned lessons to {lessons_file}",
    }
