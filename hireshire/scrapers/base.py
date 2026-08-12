from __future__ import annotations

from abc import ABC, abstractmethod

from hireshire.models.job import Job


class AbstractScraper(ABC):
    source: str

    @abstractmethod
    async def fetch_all(self, board_token: str) -> list[Job]:
        """Fetch every open job for a company. Returns [] if not found on this platform."""
        ...
