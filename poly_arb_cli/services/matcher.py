from __future__ import annotations

import difflib
from typing import Iterable, List

from ..types import MatchedMarket, Market


def match_markets(polymarkets: Iterable[Market], opinion_markets: Iterable[Market], threshold: float = 0.6) -> List[MatchedMarket]:
    """Naive matcher: pair markets with highest title similarity above threshold."""
    matches: List[MatchedMarket] = []
    used_opinion_ids: set[str] = set()

    for pm in polymarkets:
        best_match: tuple[float, Market] | None = None
        for op in opinion_markets:
            if op.market_id in used_opinion_ids:
                continue
            ratio = difflib.SequenceMatcher(a=pm.title.lower(), b=op.title.lower()).ratio()
            if ratio >= threshold and (best_match is None or ratio > best_match[0]):
                best_match = (ratio, op)
        if best_match:
            similarity, op_market = best_match
            used_opinion_ids.add(op_market.market_id)
            matches.append(MatchedMarket(polymarket=pm, opinion=op_market, similarity=similarity))
    return matches
