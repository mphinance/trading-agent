"""Hermetic tests for mcp_server/registry.py."""

from __future__ import annotations

import pytest
from mcp_server.registry import (
    _out,
    register_momentum_tools,
    register_tier1_tools,
    register_tier2_tools,
    register_tier3_tools,
)


class MockMCP:
    """Mock FastMCP instance to record registered tools without side effects."""

    def __init__(self):
        self.tools: dict[str, callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


class TestRegistryOutHelper:
    def test_out_model_dump(self):
        class ModelWithDump:
            def model_dump(self):
                return {"val": 1}

        assert _out(ModelWithDump()) == {"val": 1}

    def test_out_to_dict(self):
        class ModelWithToDict:
            def to_dict(self):
                return {"val": 2}

        assert _out(ModelWithToDict()) == {"val": 2}

    def test_out_dict_method(self):
        class ModelWithDict:
            def dict(self):
                return {"val": 3}

        assert _out(ModelWithDict()) == {"val": 3}

    def test_out_plain_value(self):
        assert _out({"key": "val"}) == {"key": "val"}
        assert _out("plain_string") == "plain_string"
        assert _out(42) == 42


class TestRegisterMomentumTools:
    def test_register_all_tiers(self):
        mock_mcp = MockMCP()
        all_tools = register_momentum_tools(mock_mcp, include_tiers=(1, 2, 3))

        # Expected 47 tools
        assert len(all_tools) == 47
        # Tool names must be unique
        assert len(all_tools) == len(set(all_tools))
        # Tools registered on the server must match returned list
        assert set(mock_mcp.tools.keys()) == set(all_tools)

    def test_tier_subsets(self):
        m1 = MockMCP()
        t1 = register_tier1_tools(m1)
        assert len(t1) == len(set(t1))
        assert len(m1.tools) == len(t1)

        m2 = MockMCP()
        t2 = register_tier2_tools(m2)
        assert len(t2) == len(set(t2))
        assert len(m2.tools) == len(t2)

        m3 = MockMCP()
        t3 = register_tier3_tools(m3)
        assert len(t3) == len(set(t3))
        assert len(m3.tools) == len(t3)

        # Sum of individual tiers must equal full registration
        m_all = MockMCP()
        all_tools = register_momentum_tools(m_all, include_tiers=(1, 2, 3))
        assert len(all_tools) == len(t1) + len(t2) + len(t3)

        # Requesting only tier 1 registers fewer tools
        m_t1_only = MockMCP()
        t1_registered = register_momentum_tools(m_t1_only, include_tiers=(1,))
        assert len(t1_registered) == len(t1)
        assert len(t1_registered) < len(all_tools)
