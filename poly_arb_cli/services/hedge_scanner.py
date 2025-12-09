"""基于衍生品隐含概率的中性对冲机会扫描器。"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from ..clients.perp import PerpClient
from ..clients.polymarket import PolymarketClient
from ..services.barrier_pricing import no_touch_prob, one_touch_prob
from ..types import HedgeMarketConfig, HedgeOpportunity, Market


def load_hedge_markets(path: Path) -> List[HedgeMarketConfig]:
    """从 JSON 文件加载对冲市场映射。

    Args:
        path: 映射文件路径，内容为数组，每项包含 market_id、underlying_symbol、
            strike、expiry、yes_on_above、est_vol 字段。

    Returns:
        解析后的 `HedgeMarketConfig` 列表；文件不存在或为空时返回空列表。
    """
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    results: List[HedgeMarketConfig] = []
    if not isinstance(raw, list):
        return results
    for item in raw:
        try:
            results.append(
                HedgeMarketConfig(
                    market_id=str(item["market_id"]),
                    underlying_symbol=str(item["underlying_symbol"]),
                    strike=float(item["strike"]),
                    expiry=str(item["expiry"]),
                    yes_on_above=bool(item.get("yes_on_above", True)),
                    est_vol=float(item["est_vol"]) if "est_vol" in item else None,
                    payoff_type=str(item.get("payoff_type", "digital")),
                    barrier=str(item.get("barrier", "up")),
                    drift=float(item.get("drift", 0.0)),
                )
            )
        except Exception:
            continue
    return results


async def scan_hedged_opportunities(
    pm_client: PolymarketClient,
    perp_client: PerpClient,
    mappings: Iterable[HedgeMarketConfig],
    *,
    pm_limit: int = 200,
    min_edge_percent: Optional[float] = None,
    default_vol: float = 1.0,
    min_gap_sigma: float = 0.2,
    use_realized_vol: bool = True,
    vol_timeframe: str = "1h",
    vol_lookback_days: int = 7,
    vol_max_candles: int = 500,
) -> List[HedgeOpportunity]:
    """扫描可对冲的标的型市场，比较 PM 价格与衍生品隐含概率。

    Args:
        pm_client: Polymarket 客户端。
        perp_client: 衍生品行情客户端。
        mappings: 市场与标的映射配置。
        pm_limit: 拉取的 Polymarket 市场数量上限。
        min_edge_percent: 过滤绝对边际收益的最小阈值，None 表示不过滤。
        default_vol: 缺省年化波动率。
        min_gap_sigma: spot 与 barrier 的最小距离（单位：sigma * sqrt(T)），
            避免几乎必触及/必不触及导致数值不稳。
        use_realized_vol: 是否尝试基于 OHLCV 计算历史波动率。
        vol_timeframe: 计算波动率的 K 线周期。
        vol_lookback_days: 向前回溯天数。
        vol_max_candles: 拉取 K 线的最大条数。

    Returns:
        按绝对边际收益排序的 `HedgeOpportunity` 列表。
    """
    pm_markets = await pm_client.list_active_markets(limit=pm_limit)
    index: dict[str, Market] = {m.market_id: m for m in pm_markets}
    now = datetime.now(tz=timezone.utc)

    results: List[HedgeOpportunity] = []
    for mapping in mappings:
        market = index.get(mapping.market_id)
        if not market:
            continue

        quote = await pm_client.get_best_prices(market)
        pm_yes = quote.yes_price
        pm_no = quote.no_price

        try:
            spot = await perp_client.fetch_mark_price(mapping.underlying_symbol)
        except Exception:
            continue

        prob_source = "digital"
        prob_above: Optional[float]
        prob: Optional[float]
        years: float

        vol = mapping.est_vol or default_vol
        if use_realized_vol:
            tf = mapping.vol_timeframe or vol_timeframe
            lb_days = mapping.vol_lookback_days or vol_lookback_days
            cache_key = (mapping.underlying_symbol, tf, lb_days)
            if cache_key not in vol_cache:
                vol_cache[cache_key] = await perp_client.fetch_realized_vol(
                    mapping.underlying_symbol,
                    timeframe=tf,
                    lookback_days=lb_days,
                    max_candles=vol_max_candles,
                )
            if vol_cache[cache_key]:
                vol = vol_cache[cache_key] or vol

        if mapping.payoff_type == "touch":
            prob, years = _implied_touch_prob(
                spot=spot,
                barrier=mapping.strike,
                expiry=mapping.expiry,
                now=now,
                vol=vol,
                drift=mapping.drift,
                direction=mapping.barrier,
                min_gap_sigma=min_gap_sigma,
            )
            prob_above = prob
            prob_source = "touch"
        elif mapping.payoff_type == "no_touch":
            prob, years = _implied_touch_prob(
                spot=spot,
                barrier=mapping.strike,
                expiry=mapping.expiry,
                now=now,
                vol=vol,
                drift=mapping.drift,
                direction=mapping.barrier,
                min_gap_sigma=min_gap_sigma,
                no_touch=True,
            )
            prob_above = prob
            prob_source = "no_touch"
        else:
            prob_above, years = _implied_prob_above(
                spot=spot,
                strike=mapping.strike,
                expiry=mapping.expiry,
                now=now,
                vol=vol,
            )
            prob_source = "digital"

        if prob_above is None:
            continue

        implied_yes = prob_above if mapping.yes_on_above else 1 - prob_above
        edge_pct = (implied_yes - pm_yes) * 100
        if min_edge_percent is not None and abs(edge_pct) < min_edge_percent:
            continue

        funding = await perp_client.fetch_funding_rate(mapping.underlying_symbol)
        note = "到期时间过短，概率可能失真" if years < (2 / 365) else None
        results.append(
            HedgeOpportunity(
                market=market,
                underlying_symbol=mapping.underlying_symbol,
                pm_yes=pm_yes,
                pm_no=pm_no,
                implied_yes=implied_yes,
                edge_percent=edge_pct,
                underlying_price=spot,
                strike=mapping.strike,
                expiry=mapping.expiry,
                funding_rate=funding,
                note=note,
                prob_source=prob_source,
                barrier=mapping.barrier if mapping.payoff_type in {"touch", "no_touch"} else None,
            )
        )

    return sorted(results, key=lambda x: abs(x.edge_percent), reverse=True)


def _implied_prob_above(spot: float, strike: float, expiry: str, now: datetime, vol: float) -> tuple[Optional[float], float]:
    """用简化的数字期权近似计算概率。

    Args:
        spot: 当前标的价格。
        strike: 阈值。
        expiry: 到期时间 ISO 字符串。
        now: 当前时间，便于测试注入。
        vol: 年化波动率（>=0）。

    Returns:
        (概率, 剩余年份) 二元组；若输入无效则返回 (None, 0)。
    """
    expiry_dt = _parse_expiry(expiry)
    if expiry_dt is None or spot <= 0 or strike <= 0:
        return None, 0.0

    seconds = (expiry_dt - now).total_seconds()
    if seconds <= 0:
        return None, 0.0
    years = seconds / (365.0 * 24 * 3600)
    sigma = max(vol, 1e-6)
    denom = sigma * math.sqrt(years)
    if denom <= 0:
        return None, years
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * years) / denom
    return _norm_cdf(d2), years


def _parse_expiry(value: str) -> Optional[datetime]:
    """解析 ISO 到期时间。

    Args:
        value: ISO8601 字符串，末尾可带 ``Z``。

    Returns:
        转换为 UTC 的 datetime；解析失败则返回 None。
    """
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        return None


def _norm_cdf(x: float) -> float:
    """正态分布累积函数。

    Args:
        x: 自变量。

    Returns:
        累积概率值。
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _implied_touch_prob(
    spot: float,
    barrier: float,
    expiry: str,
    now: datetime,
    vol: float,
    drift: float,
    direction: str,
    min_gap_sigma: float,
    no_touch: bool = False,
) -> tuple[Optional[float], float]:
    """计算触及/未触及概率。

    Args:
        spot: 当前标的价格。
        barrier: 触及阈值。
        expiry: 到期时间。
        now: 当前时间。
        vol: 年化波动率。
        drift: 年化漂移。
        direction: up/down。
        min_gap_sigma: 最小距离筛选（单位 sigma sqrt(T)）。
        no_touch: True 返回未触及概率，False 返回触及概率。

    Returns:
        (概率, 剩余年份)。失败时概率为 None。
    """
    expiry_dt = _parse_expiry(expiry)
    if expiry_dt is None or spot <= 0 or barrier <= 0:
        return None, 0.0

    seconds = (expiry_dt - now).total_seconds()
    if seconds <= 0:
        return None, 0.0
    years = seconds / (365.0 * 24 * 3600)
    sigma = max(vol, 1e-6)
    # 筛掉距离过近导致数值不稳的情况
    gap = abs(math.log(spot / barrier))
    if gap < min_gap_sigma * sigma * math.sqrt(years):
        return None, years

    if no_touch:
        prob = no_touch_prob(spot, barrier, years, sigma, drift=drift, direction=direction)
    else:
        prob = one_touch_prob(spot, barrier, years, sigma, drift=drift, direction=direction)
    return prob, years
    vol_cache: dict[tuple[str, str, int], Optional[float]] = {}
