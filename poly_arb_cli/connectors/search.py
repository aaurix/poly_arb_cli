from __future__ import annotations

from typing import List


class WebSearchConnector:
    """
    Placeholder web search connector (e.g., Tavily/DuckDuckGo/Bing).
    Implement provider-specific calls when keys/endpoints are configured.
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key

    async def search(self, query: str, limit: int = 5) -> List[dict]:
        # TODO: integrate actual search provider.
        return []
