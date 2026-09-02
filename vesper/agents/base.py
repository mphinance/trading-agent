"""Base class and protocol for Vesper specialist worker agents."""

from __future__ import annotations

import abc
import logging
from typing import Any, Dict, Optional
from vesper.state import TradingState, WorkerReport

logger = logging.getLogger(__name__)


class BaseSpecialistAgent(abc.ABC):
    """Abstract base class for all Vesper specialist analyst agents."""

    name: str = "base_agent"
    specialty: str = "general"

    @abc.abstractmethod
    async def analyze(self, ticker: str, state: TradingState) -> WorkerReport:
        """Run deep specialist analysis on a target candidate ticker.

        Args:
            ticker: The stock/asset symbol to evaluate.
            state: Current trading state with regime, candidates, technicals, etc.

        Returns:
            WorkerReport with direction, confidence score, catalysts, and invalidation levels.
        """
        raise NotImplementedError
