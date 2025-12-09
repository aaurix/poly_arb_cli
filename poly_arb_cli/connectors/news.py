from __future__ import annotations

from typing import List

import httpx


class NewsConnector:
    """
    Minimal news fetcher placeholder.
    Implement with a concrete provider (e.g., NewsAPI, custom RSS) when API keys are available.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = base_url
        self.api_key = api_key

    async def fetch(self, query: str, limit: int = 5) -> List[dict]:
        """
        Placeholder async fetch; returns empty list until wired to a provider.
        """
        # TODO: integrate a real news API; requires provider key and endpoint.
        return []
