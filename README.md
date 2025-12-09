# Polymarket–Opinion Arbitrage CLI & TUI

基于 **Polymarket Gamma/CLOB + Data-API + Opinion Open API & CLOB SDK** 的 Python CLI/TUI：

- 实时获取 Polymarket / Opinion 市场、盘口、成交数据；
- 扫描 Polymarket–Opinion 跨盘无风险套利（`PM_NO+OP_YES < 1`、`PM_YES+OP_NO < 1`）；
- 提供交互式 CLI 与 Rich / Textual 可视化；
- 预留 LangChain 1.x Agent & RAG 扩展能力。

详细架构说明见：`docs/architecture.md`。

---

## 1. 快速开始

### 环境准备

- Python 3.10+
- 推荐：
  ```bash
  python -m venv .venv
  source .venv/bin/activate
  poetry install
  ```

### 必要配置（.env）

```bash
# Polymarket CLOB / Data-API / Gamma
POLYMARKET_BASE_URL=https://gamma-api.polymarket.com
POLYMARKET_CLOB_URL=https://clob.polymarket.com
POLYMARKET_DATA_URL=https://data-api.polymarket.com

# CLOB API 凭证（推荐从 Builder Profile 获取）
POLYMKT_CLOB_API_KEY=
POLYMKT_CLOB_API_SECRET=
POLYGON_CLOB_API_PASSPHRASE=

# 可选：通过私钥推导 CLOB API 凭证
POLYGON_WALLET_PRIVATE_KEY=

# Opinion Open API & CLOB SDK
OPINION_API_KEY=
OPINION_PRIVATE_KEY=
OPINION_HOST=https://proxy.opinion.trade:8443

# 运行参数
SCAN_INTERVAL_SECONDS=60
MAX_TRADE_SIZE=50
MIN_TRADE_SIZE=5
DEFAULT_QUOTE_SIZE=10
MAX_SLIPPAGE_BPS=150
MIN_PROFIT_PERCENT=1.0
LOG_LEVEL=INFO
```

所有配置在代码中统一通过 `Settings`（`poly_arb_cli/config.py`）加载。

---

## 2. CLI 命令一览

所有命令通过 `poly-arb` 入口调用：

```bash
poetry run poly-arb <command> [options...]
```

### 2.1 市场相关

- **list-markets**：列出活跃市场（支持按成交量/流动性排序）

  ```bash
  poetry run poly-arb list-markets \
    --platform polymarket|opinion|all \
    --limit 20 \
    --sort volume|liquidity|none
  ```

  - Polymarket 侧会显示：
    - `24h Volume`：24 小时 CLOB 成交量（`volume24hrClob`）；
    - `Liquidity`：当前 CLOB 流动性（`liquidityClob`）。
  - 默认 `--sort volume`，即按 24h 成交量降序。

- **search-markets**：按标题 / slug / ID 搜索市场

  ```bash
  poetry run poly-arb search-markets "fed-rate-hike-in-2025" --platform polymarket --limit 10
  poetry run poly-arb search-markets "516706" --platform polymarket
  poetry run poly-arb search-markets "0x4319532e18" --platform polymarket
  ```

  支持匹配：
  - 标题关键字（空格 + slug 风格 `-`）；
  - 数值 ID（`market.id`）；
  - `conditionId` 前缀。

### 2.2 价格 & 盘口

- **price**：查看某个市场的 YES/NO 最优价格 + 近端流动性

  ```bash
  poetry run poly-arb price <market_id> --platform polymarket|opinion
  ```

  - Polymarket：基于 CLOB orderbook 的最优买入价 + 前几档深度；
  - Opinion：基于 Open API / SDK 盘口。

- **orderbook**：查看指定市场的订单簿深度

  ```bash
  poetry run poly-arb orderbook <market_id> \
    --platform polymarket|opinion \
    --depth 10
  ```

  分别展示 YES / NO 两侧的 BID/ASK 阶梯。

### 2.3 实时成交（tape）

- **trades-tape**：实时 Polymarket 大额成交监控（默认 WS，必要时自动回退 Data-API）

  ```bash
  poetry run poly-arb trades-tape \
    --min-notional 1000 \
    --interval 5 \
    --window 50
  ```

  - 优先使用 CLOB MARKET WebSocket 的 `book` + `last_trade_price` 更新本地 state；
  - 若 WS 状态中尚无成交，则回退到 Data-API `/trades`；
  - 表头包含：
    - 时间（UTC）、`Market`（`数值 ID | 标题`）、Outcome（YES/NO）、Side、Size、Price、Notional。

### 2.4 套利扫描 & 机器人

- **scan-arb**：执行一次跨盘套利扫描

  ```bash
  poetry run poly-arb scan-arb \
    --limit 50 \
    --threshold 0.6
  ```

  - 逻辑详见 `services/scanner.py`：
    - 使用 matcher 将 Polymarket / Opinion 按标题匹配；
    - 对每个匹配对模拟深度成交，计算：
      - `PM_NO + OP_YES`、`PM_YES + OP_NO` 的组合成本；
      - 滑点、最小成交量、最小收益率过滤；
    - 返回按收益率排序的套利机会。

- **run-bot**：持续扫描套利机会（支持 WS 行情）

  ```bash
  poetry run poly-arb run-bot \
    --interval 30 \
    --threshold 0.6 \
    --use-ws
  ```

  - 开启 `--use-ws` 时：
    - 启动 `MarketWsFeed` 订阅所有活跃市场 YES/NO token 的 CLOB MARKET channel；
    - `scan_once` 优先从 `PolymarketStreamState` 获取 OrderBook，缺失时回退 REST；
  - 使用 Rich Live 表格实时刷新套利机会。

- **scan-hedge**：Polymarket vs Perp 对冲机会扫描（BTC/ETH 类）

  ```bash
  poetry run poly-arb scan-hedge \
    --map-path data/underlying_map.json \
    --pm-limit 200 \
    --min-edge 2.0 \
    --exchange binance \
    --no-realized-vol
  ```

  - 将预测市场的概率与衍生品隐含概率对比，寻找对冲机会；
  - 默认会尝试通过 ccxt 抓取 OHLCV 计算历史波动率，可用 `--no-realized-vol` 关闭；也可用 `--vol` 提供固定年化波动率；
  - 细节见 `poly_arb_cli/services/hedge_scanner.py` 与 `data/underlying_map.sample.json`。

- **tail-watch**：Polymarket 尾盘扫货（单盘时间价值套利）监控

  ```bash
  poetry run poly-arb tail-watch \
    --interval 30 \
    --limit 500 \
    --use-ws \
    --min-price 0.95 \
    --min-yield 0.1 \
    --max-hours 72 \
    --min-notional 5000
  ```

  核心逻辑（见 `poly_arb_cli/services/tail_scanner.py`）：

  - 从 Gamma 拉取活跃市场，并解析每个市场的结束/结算时间 `end_date`；
  - 仅保留：
    - 有 YES token（可在 CLOB 上直接扫货）；
    - 距离结算时间在 `tail_max_hours_to_resolve` 小时以内（默认 72h）；
    - YES 最优买入价 `>= tail_min_yes_price`（默认 0.95）；
    - 名义扫货金额 `>= tail_min_notional`（默认 5000 美元）；
    - 预期收益率（忽略时间价值） `>= tail_min_yield_percent`；
    - 年化收益率（按剩余时间折算） `>= tail_min_annualized_yield_percent`。
  - 预期收益率估算：
    - 假设结算时 YES 收益为 1（扣除手续费后的净值为 `1 - fee`）；
    - 毛利约为 `(1 - price) * (1 - tail_fee_rate)`；
    - 预期收益率 ≈ `((1 - price) * (1 - fee) / price) * 100%`；
    - 年化收益率 ≈ `预期收益率 * 365 / (剩余天数)`（简单线性折算）。
  - WebSocket 行情：
    - 默认开启 `--use-ws`，使用 `PolymarketStreamState + MarketWsFeed` 维护本地 OrderBook；
    - 仅当 WS 尚无盘口数据时才回退到 CLOB REST `get_orderbook`。

  表格字段说明：

  - `YES Price`：当前 YES 最优买入价；
  - `Size`：在最大扫单数量 / 盘口前几档深度限制下可扫的 YES 数量；
  - `Notional`：`YES Price * Size`；
  - `Yield %`：单次尾盘扫货的预期收益率（不含时间价值）；
  - `Ann. %`：按剩余时间折算的简单年化收益率（辅助识别更优套利机会）；
  - `Hours`：距离结算的剩余小时数；
  - `Flags`：风险标签，例如：
    - `long_horizon`：剩余时间较长（> 24 小时），黑天鹅风险暴露时间更长；
    - `thin_book`：盘口较薄，扫单时可能存在额外滑点。

  该命令只负责识别和展示时间价值套利机会，不会自动下单。建议在实盘中结合自身仓位管理、黑天鹅风险偏好以及上游赛事实时信息进行综合判断。

### 2.5 账户 / 持仓

- **positions**：查看账户余额/持仓

  ```bash
  poetry run poly-arb positions --platform all|polymarket|opinion
  ```

  - Polymarket：目前为占位（需要进一步接 CLOB / 链上余额）；
  - Opinion：通过 CLOB SDK 的 `get_my_balances`。

### 2.6 可视化 TUI & Agent

- **tui**：Textual TUI 仪表盘

  ```bash
  poetry run poly-arb tui --limit 20 --threshold 0.6
  ```

  - 以 Textual 构建的终端 UI，展示套利机会列表；
  - 使用 `scan_once` 作为数据源。

- **agent**：基于 LangChain 1.x / LangGraph 的 Agentic RAG（Graph + retriever + LLM）

  ```bash
  # 自动模式（文档 / 市场统一由 Agentic RAG Graph 判定）
  poetry run poly-arb agent "列出成交量最大的 Polymarket 市场"

  # 强制偏向文档问答
  poetry run poly-arb agent "agent 命令怎么用" --mode docs

  # 强制偏向市场研究
  poetry run poly-arb agent "找和 isreal-lebanon 相关的市场" --mode markets

  # 显式指定使用 LangGraph Agentic RAG（包含 classify→rewrite→retrieve→grade→answer→check）
  poetry run poly-arb agent "解释 run-bot 参数" --mode graph
  ```

  模式说明：

  - `docs`：在 Graph 中将 route 提示为 docs，偏向文档问答；
  - `markets`：在 Graph 中将 route 提示为 markets，偏向市场研究；
  - `graph`：显式使用 LangGraph Agentic RAG（与 auto 类似，但不做额外 heuristics）；
  - `auto`（默认）：由 classify 节点自动判定 docs/markets，若问题包含文档关键词则偏向 docs。

  索引构建（建议先跑）：

  ```bash
  # 构建文档向量索引（data/chroma_docs）
  poetry run poly-arb build-docs-index

  # 构建市场语义索引（data/chroma_markets），优先索引活跃市场
  poetry run poly-arb build-markets-index \
    --limit 2000 \
    --sort volume \        # 或 liquidity
    --min-volume 1000 \    # 可选：过滤 24h 成交量 < 1000 的市场
    --min-liquidity 500    # 可选：过滤流动性 < 500 的市场
  ```

  模型与 Embeddings：
  - 从 `.env` / `Settings` 读取：`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`（默认 gpt-4o-mini）、`EMBEDDING_MODEL`（默认 text-embedding-3-large）。

---

## 3. 目录与核心模块

- `pyproject.toml`：项目依赖、脚本入口（`poly-arb = poly_arb_cli.cli:main`）
- `poly_arb_cli/config.py`：配置模型 `Settings`（基于 pydantic-settings）
- `poly_arb_cli/types.py`：核心数据模型：
  - `Market / OrderBook / OrderBookLevel / PriceQuote`
  - `MatchedMarket / ArbOpportunity / TradeResult / TradeEvent`
  - `HedgeMarketConfig / HedgeOpportunity`
- `poly_arb_cli/clients/`：
  - `polymarket.py`：Gamma / CLOB / Data-API 客户端；
  - `opinion.py`：Opinion Open API + CLOB SDK 客户端；
  - `perp.py`：用于对冲扫描的 perp 交易所客户端（ccxt 包装）。
- `poly_arb_cli/connectors/`：
  - `polymarket_ws.py`：Polymarket MARKET WebSocket feed + 本地 state；
  - `news.py / search.py / vector.py`：RAG / 外部数据源占位。
- `poly_arb_cli/services/`：
  - `matcher.py`：Polymarket–Opinion 标题匹配；
  - `pricing.py`：深度成交模拟、最优价、滑点计算；
  - `scanner.py`：跨盘套利扫描（支持 WS state）；
  - `hedge_scanner.py`：对冲机会扫描；
  - `trader.py`：交易执行骨架（后续接实盘）。
- `poly_arb_cli/ui/`：
  - Textual 仪表盘与 CLI 视觉组件。
- `poly_arb_cli/llm/`：
  - LangChain 1.x Agent & Tools。
- `poly_arb_cli/storage.py`：
  - JSONL 日志落盘（策略机会、对冲机会等）。
- `docs/requirements_architecture.md`：
  - 项目需求与高层架构说明。
- `docs/architecture.md`：
  - 详细的架构 / 数据流 / 分层设计（本次新增，见下文）。

---

## 4. 测试与代码质量

- 代码格式与静态检查：
  ```bash
  poetry run ruff check .
  poetry run mypy .
  ```
- 单元测试：
  ```bash
  poetry run pytest
  ```

建议重点补充测试：matcher、scanner、pricing、WS feed 状态更新等核心模块。
