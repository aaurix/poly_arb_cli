from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from env or .env file."""

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
