import pytest
from pathlib import Path
from vesper.leveraged import get_leveraged_etfs, get_primary_2x
from vesper.skills_engine import create_new_skill, SKILLS_DIR
from vesper.state import TechnicalAudit, MarketRegime, TradingState
from vesper.nodes.playbooks import playbooks_node


def test_leveraged_etf_lookup():
    nvda_etfs = get_leveraged_etfs("NVDA")
    assert len(nvda_etfs) > 0
    assert any("NVD" in e["etf_ticker"] for e in nvda_etfs)

    primary_nvda = get_primary_2x("NVDA")
    assert primary_nvda is not None
    assert "NVD" in primary_nvda

    primary_tsla = get_primary_2x("TSLA")
    assert primary_tsla == "TSLL"


@pytest.mark.asyncio
async def test_playbooks_node_generates_2x_leveraged_proposal():
    state: TradingState = {
        "session_id": "test-session",
        "mode": "dry_run",
        "selected_playbook": "all",
        "target_ticker": "NVDA",
        "regime": MarketRegime(
            posture="NEUTRAL",
            health_score=4.0,
            spy_spot=580.0,
            spy_gamma_flip=575.0,
        ),
        "candidates": [],
        "technicals": {
            "NVDA": TechnicalAudit(
                ticker="NVDA",
                close=120.0,
                rsi_14=65.0,
                rsi_state="NEUTRAL",
                ema_stack="BULLISH",
                atr_14=4.0,
                composite_score=8.5,
            )
        },
        "options_audits": {},
        "proposals": [],
        "risk_assessments": [],
        "human_decision": None,
        "execution_results": [],
        "reflection_notes": [],
        "audit_trail": [],
    }

    res = await playbooks_node(state)
    props = res.get("proposals", [])
    assert len(props) >= 2  # Primary NVDA equity + 2x leveraged alternate

    # Verify primary equity proposal
    nvda_prop = next(p for p in props if p.ticker == "NVDA")
    assert nvda_prop.asset_type == "EQUITY"
    assert nvda_prop.side == "BUY"

    # Verify 2x leveraged proxy proposal
    proxy_prop = next(p for p in props if p.asset_type == "LEVERAGED_ETF")
    assert proxy_prop is not None
    assert proxy_prop.quantity < nvda_prop.quantity  # Scaled down position for equal risk budget
    assert proxy_prop.max_risk < nvda_prop.max_risk


def test_skills_engine_skill_creation():
    # Test safe skill creation
    res = create_new_skill(
        name="test-vcp-evolution",
        description="Autonomous evolved VCP volatility breakout skill",
        content_markdown="""# Test Evolved Skill
1. Identify 3+ contractions.
2. Volume dries up < 50-day average.
3. Trigger buy when price crosses pivot line.
""",
    )
    assert res["status"] == "success"
    skill_md = Path(res["path"])
    assert skill_md.exists()
    content = skill_md.read_text()
    assert "name: test-vcp-evolution" in content

    # Clean up test skill
    import shutil
    shutil.rmtree(skill_md.parent, ignore_errors=True)

    # Test invalid path traversal rejection
    bad_res = create_new_skill(
        name="../../etc/passwd",
        description="Exploit attempt",
        content_markdown="danger",
    )
    assert bad_res["status"] == "error"
    assert "Invalid skill name" in bad_res["message"]
