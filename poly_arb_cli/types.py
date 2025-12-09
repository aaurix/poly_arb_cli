from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Platform(str, Enum):
    POLYMARKET = "polymarket"
    OPINION = "opinion"


@dataclass
class Tag:
    """Polymarket Gamma 标签元数据。

    Attributes:
        id: 标签唯一 ID。
        label: 展示名称，如 \"Politics\"。
        slug: 机器友好的短名称，如 \"politics\"。
    """

    id: str
    label: str
    slug: str


@dataclass
class Market:
    """统一描述各平台市场元数据的数据类。

    Attributes:
        platform: 平台标识，当前支持 polymarket / opinion。
        market_id: 人类友好的市场 ID（如 Gamma `id`）。
        title: 市场标题或问题文本。
        condition_id: Polymarket 条件 ID（若存在）。
        end_date: 到期时间（如有），通常为 ISO8601 字符串。
        yes_token_id: YES 合约的 token id（CLOB/盘口使用）。
        no_token_id: NO 合约的 token id（CLOB/盘口使用）。
        category: 归类标签，Polymarket 上通常来自 category 字段或首个 tag。
        volume: 24 小时成交量（若平台提供）。
        liquidity: 当前流动性指标（若平台提供）。
        tags: 原始标签列表（如 Polymarket Gamma `tags` 字段）。
    """

    platform: Platform
    market_id: str
    title: str
    condition_id: Optional[str] = None
    end_date: Optional[str] = None
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    category: Optional[str] = None
    volume: Optional[float] = None
    liquidity: Optional[float] = None
    tags: Optional[list[str]] = None


@dataclass
class PriceQuote:
    yes_price: float
    no_price: float
    spread_bps: Optional[float] = None
    yes_liquidity: Optional[float] = None
    no_liquidity: Optional[float] = None


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

    def best_bid(self) -> Optional[OrderBookLevel]:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> Optional[OrderBookLevel]:
        return self.asks[0] if self.asks else None


@dataclass
class MatchedMarket:
    polymarket: Market
    opinion: Market
    similarity: Optional[float] = None


@dataclass
class ArbOpportunity:
    pair: MatchedMarket
    route: str  # "PM_NO + OP_YES" or "PM_YES + OP_NO"
    cost: float
    profit_percent: float
    size: Optional[float] = None
    max_size: Optional[float] = None
    price_breakdown: Optional[str] = None


@dataclass
class Position:
    platform: Platform
    token_id: str
    symbol: str
    balance: float
    available: Optional[float] = None


@dataclass
class TradeLegResult:
    platform: Platform
    market_id: str
    side: str
    price: float
    size: float
    order_id: Optional[str] = None
    status: str = "unknown"
    error: Optional[str] = None


@dataclass
class TradeResult:
    opportunity: ArbOpportunity
    pm_leg: TradeLegResult
    op_leg: TradeLegResult
    success: bool
    notes: Optional[str] = None


@dataclass
class TradeEvent:
    """Polymarket 单笔成交事件。

    Attributes:
        condition_id: 条件 ID（market/condition 标识）。
        token_id: 资产 ID（tokenId）。
        side: 成交方向，BUY 或 SELL。
        size: 成交数量（份数）。
        price: 成交价格（0-1 间小数）。
        notional: 名义金额，通常为 size * price。
        timestamp: Unix 时间戳（秒）。
        title: 市场标题。
        outcome: 具体 outcome 名称（如 Yes/No 或 Up/Down）。
        tx_hash: 交易哈希。
        wallet: 用户 proxy 钱包地址。
        pseudonym: 用户昵称（如有）。
    """

    condition_id: str
    token_id: str
    side: str
    size: float
    price: float
    notional: float
    timestamp: int
    title: str
    outcome: Optional[str] = None
    tx_hash: Optional[str] = None
    wallet: Optional[str] = None
    pseudonym: Optional[str] = None

    @property
    def dt(self) -> datetime:
        """将 Unix 时间戳转换为 UTC datetime。"""

        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)


@dataclass
class HedgeMarketConfig:
    """描述与标的衍生品绑定的预测市场元数据。

    Attributes:
        market_id: Polymarket 市场 ID。
        underlying_symbol: ccxt 符号（如 ``BTC/USDT:USDT``）。
        strike: 价格阈值，YES/NO 的判定基准。
        expiry: 事件到期时间，ISO8601 字符串（UTC）。
        yes_on_above: YES 是否代表价格高于阈值。
        est_vol: 预设年化波动率，缺省时使用全局默认。
        payoff_type: 收益类型，支持数字（到期结算）和触及（one-touch/no-touch）。
        barrier: 触及方向，上/下。
        drift: 标的年化漂移，用于触及定价，默认 0。
    """

    market_id: str
    underlying_symbol: str
    strike: float
    expiry: str
    yes_on_above: bool = True
    est_vol: Optional[float] = None
    payoff_type: str = "digital"  # digital | touch | no_touch
    barrier: str = "up"  # up | down
    drift: float = 0.0
    vol_lookback_days: Optional[int] = None
    vol_timeframe: Optional[str] = None


@dataclass
class HedgeOpportunity:
    """中性对冲扫描机会的描述体。

    Attributes:
        market: 匹配到的 Polymarket 市场。
        underlying_symbol: 衍生品使用的标的符号。
        pm_yes: Polymarket YES 报价。
        pm_no: Polymarket NO 报价。
        implied_yes: 由标的价格与波动率推导的 YES 概率。
        edge_percent: 概率差异（implied - pm_yes）百分比。
        underlying_price: 当前标的价格。
        strike: 判定阈值。
        expiry: 到期时间（ISO 字符串）。
        funding_rate: 当前资金费率（若有）。
        note: 额外提示，如波动率来源、时间过短等。
        prob_source: 概率来源（digital/touch/no_touch）。
        barrier: 若为触及类机会，记录 barrier 方向。
    """

    market: Market
    underlying_symbol: str
    pm_yes: float
    pm_no: float
    implied_yes: float
    edge_percent: float
    underlying_price: float
    strike: float
    expiry: str
    funding_rate: Optional[float] = None
    note: Optional[str] = None
    prob_source: str = "digital"
    barrier: Optional[str] = None


@dataclass
class RebalanceSignal:
    """描述单个市场再平衡监控信号的数据类。

    市场再平衡套利关注价格在短时间内偏离自身均衡水平（如
    最近均价或平滑后的基线价格）的情形。本数据类仅承载监
    控信号本身，不直接触发下单逻辑，方便 CLI 或上层策略
    进行展示与二次过滤。

    Attributes:
        market: 触发信号的 Polymarket 市场。
        direction: 建议方向，例如 ``short_yes`` 或 ``short_no``。
        current_yes: 当前估算的 YES 价格（通常取 bid/ask 中值）。
        baseline_yes: 监控窗口内平滑后的 YES 基线价格。
        delta: 当前价格与基线的差值（current_yes - baseline_yes）。
        last_trade_notional: 最近一笔成交的名义金额，用于识别鲸鱼冲击。
        window_seconds: 监控窗口长度（秒），用于描述信号参考的时间尺度。
        reason: 人类可读的简要说明，便于在终端或日志中查看。
    """

    market: Market
    direction: str
    current_yes: float
    baseline_yes: float
    delta: float
    last_trade_notional: float
    window_seconds: int
    reason: Optional[str] = None
