# Polymarket–Opinion CLI 架构与数据流说明

本项目旨在提供一套可扩展的 **Polymarket–Opinion 跨盘套利 CLI/TUI 工具**，在保持实现简单可控的前提下，遵循「分层清晰、数据模型统一、易于引入 WS/Agent/RAG」的最佳实践。

本文从整体架构、关键模块、数据流、原理与当前实现状态几个角度展开。

---

## 1. 总体架构概览

自上而下可以概括为三层：

- **接口层（Interface Layer）**
  - CLI 命令：`poly_arb_cli/cli/` 子包（基于 Click + Rich）
  - Textual TUI：`poly_arb_cli/ui/`
  - LangChain Agent：`poly_arb_cli/llm/`

- **领域服务层（Domain Services Layer）**
  - 套利扫描：`services/scanner.py`
  - 对冲扫描：`services/hedge_scanner.py`
  - 匹配 / 定价：`services/matcher.py`、`services/pricing.py`
  - 交易骨架：`services/trader.py`

- **数据接入层（Data Access & Feed Layer）**
  - HTTP 客户端：`clients/polymarket.py`、`clients/opinion.py`、`clients/perp.py`
  - WebSocket Feed & 本地状态：`connectors/polymarket_ws.py`
  - 配置 & 类型：`config.py`、`types.py`

数据流从下往上是：

> Polymarket / Opinion API & WS → Clients / Feeds → 本地状态（OrderBook / TradeEvent）→ Services（scanner/hedge_scanner）→ CLI/TUI/Agent 输出

---

## 2. 核心数据模型（types.py）

所有内部模块尽量只通过 `poly_arb_cli.types` 定义的数据类交互，避免 JSON 结构散落各处。

### 2.1 市场与盘口

- `Market`
  - `platform: Platform`（`polymarket` / `opinion`）
  - `market_id: str`：人类友好的市场 ID（Polymarket 为 Gamma `id`，Opinion 为 `marketId`/`topic_id`）
  - `title: str`：市场标题
  - `condition_id: Optional[str]`：Polymarket `conditionId`（CLOB / WS 使用）
  - `yes_token_id / no_token_id: Optional[str]`：YES/NO tokenId（CLOB 盘口的主键）
  - `volume: Optional[float]`：24h 成交量（Polymarket 侧为 `volume24hrClob`）
  - `liquidity: Optional[float]`：当前 CLOB 流动性（`liquidityClob`）

- `OrderBookLevel`
  - `price: float`
  - `size: float`

- `OrderBook`
  - `bids: list[OrderBookLevel]`
  - `asks: list[OrderBookLevel]`
  - `best_bid() / best_ask()`：返回首档。

- `PriceQuote`
  - `yes_price / no_price: float`：单价
  - `yes_liquidity / no_liquidity: Optional[float]`：前几档聚合流动性
  - `spread_bps: Optional[float]`：价差基点（当前未主动使用，但为后续扩展预留）

### 2.2 成交与套利机会

- `TradeEvent`
  - `condition_id: str`：Polymarket `conditionId`
  - `token_id: str`：资产 ID（tokenId）
  - `side: str`：`BUY` / `SELL`
  - `size: float`：成交数量（份数）
  - `price: float`：成交价格（[0, 1]）
  - `notional: float`：名义金额（`size * price`）
  - `timestamp: int`：Unix（秒）
  - `title: str`：市场标题（可选）
  - `outcome: Optional[str]`：YES/NO / Up/Down 等
  - `tx_hash / wallet / pseudonym`：用于将来结合 user/trade feed 细化显示
  - `dt: datetime` property：便于 Rich 表格展示。

- `MatchedMarket`
  - `polymarket: Market`
  - `opinion: Market`
  - `similarity: Optional[float]`：标题相似度（0–1）

- `ArbOpportunity`
  - `pair: MatchedMarket`
  - `route: str`：`PM_NO + OP_YES` 或 `PM_YES + OP_NO`
  - `cost: float`：组合成本（如 `pm_no_price + op_yes_price`）
  - `profit_percent: float`：`(1 - cost) * 100`
  - `size: Optional[float]`：实际可成交规模（基于深度模拟）
  - `max_size: Optional[float]`
  - `price_breakdown: Optional[str]`：例如 `PM_NO 0.4800 | OP_YES 0.4600`

---

## 3. 数据接入层：Polymarket & Opinion

### 3.1 PolymarketClient（Gamma / CLOB / Data-API）

文件：`poly_arb_cli/clients/polymarket.py`

职责：

- 从 Gamma 获取市场列表；
- 使用 CLOB 获取订单簿；
- 使用 Data-API 获取最近成交；
- 管理 CLOB API 凭证（用于 L2 接口）。

关键方法：

- `list_active_markets(limit: int) -> list[Market]`
  - 调用 Gamma `/markets`：
    - 参数：`active=True, closed=False, archived=False, enableOrderBook=True, limit=...`
  - 解析：
    - `id` → `market_id`
    - `conditionId` → `condition_id`
    - `question/title/name` → `title`
    - `volume24hrClob`/`volume24hr` → `Market.volume`
    - `liquidityClob`/`liquidityNum/liquidity` → `Market.liquidity`
    - `clobTokenIds`（字符串或列表） → `yes_token_id` / `no_token_id`

- `get_orderbook(market: Market, side: "yes"|"no") -> OrderBook`
  - 通过 `market.yes_token_id` / `market.no_token_id` 调用 `py_clob_client.get_order_book`；
  - 映射 CLOB 返回的 `OrderSummary(price, size)` 为 `OrderBookLevel` 列表。

- `get_best_prices(market: Market) -> PriceQuote`
  - 分别获取 YES/NO 盘口；
  - 使用 `_best_price` / `_liquidity` 计算最优买入价与近端流动性。

- `get_recent_trades(limit: int) -> list[TradeEvent]`
  - 调用 Data-API `/trades?limit=...`；
  - 将每条记录映射为 `TradeEvent`（title / outcome / wallet 等党可用字段延后再补）。

- CLOB API 凭证：
  - 若 `.env` 提供：
    - `POLYMKT_CLOB_API_KEY` / `POLYMKT_CLOB_API_SECRET` / `POLYGON_CLOB_API_PASSPHRASE`，则通过：
      ```python
      self._clob_client.set_api_creds({...})
      ```
  - 否则若有 `POLYGON_WALLET_PRIVATE_KEY`，则通过：
      ```python
      self._clob_client.set_api_creds(
          self._clob_client.create_or_derive_api_creds()
      )
      ```
  - 这为未来 L2 只读接口（如 `get_trades`）和 user WS 留好基础。

### 3.2 OpinionClient（Open API / CLOB SDK）

文件：`poly_arb_cli/clients/opinion.py`

设计原则：

- 只读市场 / 盘口优先走 Open API：
  - `GET /openapi/market`：市场列表；
  - `GET /openapi/token/orderbook`：盘口；
  - 不强依赖 CLOB SDK 和私钥。
- 下单 / 余额 / 持仓依赖 CLOB SDK（`opinion-clob-sdk==0.4.3`）。

关键方法：

- `list_active_markets(limit: int) -> list[Market]`
  - 优先：
    - 调 `GET /openapi/market?page=1&size=...&status=activated&marketType=0` 并分页；
    - 解析 `marketId`、`marketTitle`、`yesTokenId`、`noTokenId`。
  - 回退：
    - 若 Open API 不可用但 SDK 已初始化，则退回 `sdk.get_markets(status=ACTIVATED, limit=...)`。

- `get_orderbook(market, side)`：
  - 优先 `GET /openapi/token/orderbook?token_id=...`；
  - 回退 `sdk.get_orderbook(token_id)`。

- `get_best_prices(market)`：
  - 调 `get_orderbook` 再计算最优价与流动性。

- 其他（交易相关）：
  - `place_order` / `cancel_order` / `get_balances` 等，均通过 CLOB SDK 调用对应方法，并返回内部统一模型。

---

## 4. WebSocket Feed 与本地 State

文件：`poly_arb_cli/connectors/polymarket_ws.py`

目标：在不破坏原有 REST 逻辑的前提下，为 Polymarket 行情提供一个 **WS → 本地状态** 的实现，方便后续在 Scanner / Tape 中优先读取 WS 数据。

### 4.1 PolymarketStreamState

```python
@dataclass
class PolymarketStreamState:
    orderbooks: Dict[str, OrderBook]
    trades_by_condition: Dict[str, Deque[TradeEvent]]
    max_trades_per_market: int = 200
```

职责：

- `apply_book_snapshot(asset_id, bids, asks)`：
  - 将 MARKET channel 的 book 消息（bids/asks 或 buys/sells）转换为 `OrderBook` 并写入 `orderbooks[token_id]`。

- `append_last_trade(data)`：
  - 将 `last_trade_price` 消息转换为 `TradeEvent`（把毫秒时间戳转为秒），并追加到 `trades_by_condition[condition_id]` 的环形缓冲。

- `get_orderbook_for_market(market, side)`：
  - 根据 `Market.yes_token_id` / `no_token_id` 返回对应的订单簿（若存在）。

- `get_last_trades(condition_id, limit)`：
  - 获取最近 n 条成交，用于 tape 或策略分析。

### 4.2 MarketWsFeed

```python
class MarketWsFeed:
    def __init__(self, settings: Settings, state: PolymarketStreamState, asset_ids: Iterable[str]): ...
    async def run(self) -> None: ...
    def stop(self) -> None: ...
```

行为：

- 连接 `wss://ws-subscriptions-clob.polymarket.com/ws/market`；
- 发送订阅消息：
  ```json
  {
    "type": "MARKET",
    "assets_ids": ["token_id_1", "token_id_2", ...]
  }
  ```
- 解析服务端消息：
  - 消息可能是单个对象，也可能是数组，代码中统一归一化为列表；
  - 若包含 `bids/asks/buys/sells`：
    - 视为 book 快照，调用 `state.apply_book_snapshot`；
  - 若 `event_type == "last_trade_price"`：
    - 视为成交事件，调用 `state.append_last_trade`；
  - `price_changes` / `tick_size_change` 暂未使用。

- 简单重连策略：
  - 捕获异常后按 `1, 2, 4, ... 30` 秒指数退避重连。

---

## 5. 套利扫描器（Scanner）

文件：`poly_arb_cli/services/scanner.py`

函数签名：

```python
async def scan_once(
    polymarket_client: PolymarketClient,
    opinion_client: OpinionClient,
    *,
    limit: int = 50,
    threshold: float = 0.6,
    pm_state: Optional[PolymarketStreamState] = None,
) -> list[ArbOpportunity]:
```

逻辑步骤：

1. **获取市场列表**
   - `pm_markets = await polymarket_client.list_active_markets(limit=limit)`
   - `op_markets = await opinion_client.list_active_markets(limit=limit)`

2. **标题匹配**
   - `matched = match_markets(pm_markets, op_markets, threshold=threshold)`
   - matcher 目前采用简单的 token 相似度（Jaccard / cos-like），对标题、slug 做预处理。

3. **拉取盘口（支持 WS state）**
   - 对每个 `pair`：
     - Polymarket 部分：
       ```python
       if pm_state is not None:
           pm_yes_book = pm_state.get_orderbook_for_market(pair.polymarket, "yes") or OrderBook(...)
           pm_no_book = pm_state.get_orderbook_for_market(pair.polymarket, "no") or OrderBook(...)
       else:
           pm_yes_book = OrderBook(...)
           pm_no_book = OrderBook(...)

       if not pm_yes_book.bids and not pm_yes_book.asks:
           pm_yes_book = await polymarket_client.get_orderbook(pair.polymarket, side="yes")
       if not pm_no_book.bids and not pm_no_book.asks:
           pm_no_book = await polymarket_client.get_orderbook(pair.polymarket, side="no")
       ```
     - Opinion 部分：
       - 始终通过 `opinion_client.get_orderbook`（内部已负责 Open API / SDK 的回退）。

4. **深度成交模拟与套利判定**

   利用 `services/pricing.py`：

   - `compute_fill(orderbook, side="buy", size=target_size)`：
     - 从头逐档吃单，计算平均成交价与实际填充规模；
   - `best_price(orderbook, side="buy")`：
     - 提取 best ask（买入最优价）。
   - `clamp_slippage(entry_price, avg_price, max_slippage_bps)`：
     - 控制平均成交价相对于首档价的滑点。

   对每个 `pair` 计算两条路线：

   - 路线 1：`PM_NO + OP_YES`
   - 路线 2：`PM_YES + OP_NO`

   判定逻辑：

   - 两边深度可成交规模均 ≥ `settings.min_trade_size`；
   - 组合成本 `< 1`；
   - 预期收益率 `(1 - cost) * 100 ≥ settings.min_profit_percent`；
   - 两边买入均价相对 best price 的滑点 ≤ `settings.max_slippage_bps`。

5. **排序与输出**

   - 最终将所有 `ArbOpportunity` 按 `profit_percent` 降序排序返回，供 CLI / TUI / Agent 使用。

---

## 6. CLI 层与数据流

文件：`poly_arb_cli/cli/`

CLI 中大部分命令都有相似模式：

1. 加载配置：`settings = Settings.load()`；
2. 构建客户端：`pm_client, op_client = _build_clients(settings)`；
3. 调用 domain service 或 client 方法；
4. 用 Rich 渲染表格或 Live/TUI；
5. 最后关闭客户端：`await asyncio.gather(pm_client.close(), op_client.close())`。

重要命令与数据流：

- `list-markets`
  - `PolymarketClient.list_active_markets` / `OpinionClient.list_active_markets`
  - 可选 sort：`volume` / `liquidity`。

- `price`
  - `client.get_best_prices(market)` → `PriceQuote` → Rich 表格。

- `orderbook`
  - `_find_market_by_id`（在单平台列表中按 `market_id` 找市场）；
  - `client.get_orderbook(market, "yes"/"no")`；
  - `_print_orderbook`（Rich 表格）。

- `trades-tape`
  - 默认：
    - 启动 WS feed：
      - `PolymarketStreamState` + `MarketWsFeed`；
      - 从 state 读取 `trades_by_condition`；
    - 若 state 无成交，则 fallback 到 `PolymarketClient.get_recent_trades`：
  - 每个 interval：
    - 聚合、过滤（`min_notional`）、排序（按 timestamp）；
    - 更新统计（总笔数、总 notional、平均单笔）；
    - Rich Layout + Panels 渲染。

- `scan-arb` / `run-bot` / `tui`
  - 都以 `scan_once` 为数据源：
    - `run-bot` 可开启 `--use-ws`，为 Scanner 注入 WS state；
    - `tui` 将 `scan_once` 的结果涂装到 Textual 组件。

---

## 7. 当前实现状态与下一步方向

**已实现：**

- Polymarket：
  - Gamma `/markets`：当前可交易市场列表 + 24h 成交量 / 流动性；
  - CLOB `/order_book`：订单簿与价格；
  - Data-API `/trades`：最近全市场成交；
  - MARKET WebSocket：book + last_trade_price → 本地 state。

- Opinion：
  - Open API `/openapi/market`：市场列表；
  - Open API `/openapi/token/orderbook`：盘口；
  - CLOB SDK：下单、撤单、余额（骨架已接好）。

- 套利 / 对冲：
  - 基于深度成交模拟的跨盘套利扫描（含滑点 / 最小成交量 / 最小收益率控制）；
  - 基于衍生品隐含概率的对冲扫描（可选 ccxt + 预设 vol）。

- 可视化：
  - Rich CLI 表格；
  - Rich Live 实时套利表；
  - Textual TUI 仪表盘；
  - Trades tape（实时大额成交）。

- Agent & RAG：
  - LangChain 1.x Agent 包装基础工具；
  - 预留 news/search/vector connectors 用于后续 RAG。

**下一步可选方向：**

- 实盘交易路径：
  - 完整打通 Polymarket 下单 / 撤单 / 余额；
  - Opinion CLOB 下单策略与失败回滚（单边成交处理）。

- 风控与统计：
  - 增加 PnL 跟踪、持仓明细、资金曲线；
  - 增加告警（Telegram / Email / Webhook）。

- 检索与 Agent：
  - 使用 ChromaDB / 外部向量库对多平台市场做语义索引；
  - 结合新闻 / 社交媒体 / 研究报告构建 Q&A Agent；
  - 为策略提供「主动选标的」的辅助 Agent（而不仅是机械扫描）。

整体设计目标是：在保证当前 CLI 工具**稳定可用**的基础上，仍能自然地演进到 WS 驱动的高频策略平台与 LangChain/多 Agent 的全盘管理层。开发过程中建议严格复用 `types.py` 与 `clients/` 的接口，以减小未来更换数据源或引入新市场的成本。  
