"""PSP parser base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..canonical import SettlementBatch


class BaseParser(ABC):
    psp: str = ""

    @abstractmethod
    def parse(self, path: str | Path) -> SettlementBatch:
        """Parse one PSP settlement file into a canonical SettlementBatch."""
