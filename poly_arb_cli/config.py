from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时配置模型，从环境变量或 .env 加载。

    本类集中管理 CLI/服务所需的外部依赖配置，例如
    Polymarket/Opinion API 端点、CLOB 凭证、对冲与
    套利参数以及日志级别等。
    """

    # 本地数据目录，用于存放缓存/向量索引等构建产物
    data_dir: Path = Path("data")

    # LLM / Embedding 相关配置（OpenAI 兼容）
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    openai_model: str = "deepseek/deepseek-chat-v3.1"
    embedding_model: str = "qwen/qwen3-embedding-8b"

    polymarket_private_key: Optional[str] = None
    polymarket_api_key: Optional[str] = None
    polymarket_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_data_url: str = "https://data-api.polymarket.com"
    polymarket_ws_url: str = "wss://clob.polymarket.com/stream"

    # 可选：显式配置 CLOB API 凭证（builder profile 中生成的 key/secret/passphrase）
    polymkt_clob_api_key: Optional[str] = None
    polymkt_clob_api_secret: Optional[str] = None
    polygon_clob_api_passphrase: Optional[str] = None

    opinion_api_key: Optional[str] = None
    opinion_private_key: Optional[str] = None
    opinion_host: str = "https://proxy.opinion.trade:8443"
    opinion_ws_url: str = "wss://proxy.opinion.trade:8443/ws"

    rpc_url: Optional[str] = None

    # 衍生品/对冲相关配置
    perp_exchange: str = "binanceusdm"
    perp_api_key: Optional[str] = None
    perp_api_secret: Optional[str] = None
    perp_testnet: bool = False

    hedge_min_edge_percent: float = 2.0
    hedge_default_vol: float = 1.0  # 年化波动率缺省值，用于概率近似
    hedge_min_gap_sigma: float = 0.2  # spot 与 barrier 距离的最小 σ*sqrt(T)
    hedge_use_realized_vol: bool = True
    hedge_vol_timeframe: str = "1h"
    hedge_vol_lookback_days: int = 7
    hedge_vol_max_candles: int = 500

    # 尾盘扫货策略相关阈值
    tail_min_yes_price: float = 0.95
    tail_focus_price: float = 0.997
    tail_max_hours_to_resolve: float = 72.0
    tail_min_notional: float = 5000.0
    tail_min_yield_percent: float = 0.1
    tail_min_annualized_yield_percent: float = 20.0
    tail_max_sweep_size: float = 50.0
    tail_fee_rate: float = 0.02  # 预估结算费用占比

    scan_interval_seconds: int = 60
    max_trade_size: float = 50.0
    min_trade_size: float = 5.0
    default_quote_size: float = 10.0
    max_slippage_bps: int = 150  # 1.5%
    min_profit_percent: float = 1.0
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    @classmethod
    def load(cls, env_file: Optional[str | Path] = None, overrides: Optional[dict[str, Any]] = None) -> "Settings":
        """Load settings, allowing an optional .env override and programmatic overrides."""
        kwargs: dict[str, Any] = {}
        if env_file:
            kwargs["_env_file"] = env_file
        if overrides:
            kwargs.update(overrides)
        return cls(**kwargs)

    def ensure_data_dir(self) -> Path:
        """确保 data_dir 存在并返回绝对路径。"""

        path = self.data_dir
        if not path.is_absolute():
            path = Path(".").resolve() / path
        path.mkdir(parents=True, exist_ok=True)
        return path
