# CLI 分层设计与命令一览

本篇文档聚焦于 `poly_arb_cli/cli/` 子包的结构设计，解释各个子模块的职责划分、依赖关系以及与领域服务层的交互方式。整体架构与更完整的数据流说明可参考 `docs/architecture.md`。

---

## 1. CLI 总体结构

CLI 入口定义在：

- 包路径：`poly_arb_cli.cli`
- 入口函数：`main()`（`pyproject.toml` 中 `poly-arb = poly_arb_cli.cli:main`）

代码组织：

- `poly_arb_cli/cli/__init__.py`：顶层命令组定义与子命令注册；
- `poly_arb_cli/cli/common.py`：CLI 共享工具与 Rich 渲染辅助；
- `poly_arb_cli/cli/markets.py`：市场列表 / 搜索 / 盘口 / 价格；
- `poly_arb_cli/cli/arb.py`：跨盘套利扫描 / 对冲扫描 / 机器人；
- `poly_arb_cli/cli/trades.py`：成交流水（trades-tape）；
- `poly_arb_cli/cli/account.py`：余额与持仓（positions）；
- `poly_arb_cli/cli/tui_agent.py`：Textual TUI 与 LangChain Agent。

这种拆分方式的目标是：

- 保持单文件体积适中，便于维护与代码审查；
- 让每个子模块只关注一种「领域场景」（市场、套利、账户、可视化等）；
- CLI 层尽量只做参数解析与展示，将业务逻辑下沉到 `services/` 与 `clients/`。

---

## 2. 公共工具模块 `common.py`

`common.py` 中只放与 CLI 相关的共享逻辑：

- `console: Console`
  - 单例 Rich 控制台实例，所有 CLI 输出统一通过该对象；
- `build_clients(settings) -> (PolymarketClient, OpinionClient)`
  - 使用 `Settings` 构建 Polymarket / Opinion 客户端；
- `normalize_platform(value, allow_all=True) -> str`
  - 规范化平台参数（polymarket/opinion/all），错误时抛出 `click.BadParameter`；
- `matches_query(title, query) -> bool`
  - 兼容普通关键字与 slug 风格（`will-israel-strike-lebanon-on`）的标题匹配；
- `find_market_by_id(client, market_id, search_limit=500) -> Market | None`
  - 在单一平台上按 `market_id` 搜索市场；
- `print_opportunities(opportunities)`
  - 以 Rich 表格渲染套利机会；
- `print_hedge_opportunities(opportunities)`
  - 以 Rich 表格渲染对冲机会；
- `print_orderbook(label, book, depth)`
  - 渲染单侧订单簿（YES/NO）。

CLI 子模块只需关心「调用哪些服务」，而不必重复定义这些工具。

---

## 3. 市场命令 `markets.py`

模块职责：查询与展示市场信息。

提供的命令：

- `list-markets`
  - 使用 `PolymarketClient.list_active_markets` / `OpinionClient.list_active_markets`；
  - 支持 `--platform polymarket|opinion|all`；
  - 支持 `--sort volume|liquidity|none`，默认按 Polymarket 24h 成交量降序；
  - 使用 Rich 表格展示 Platform / ID / Title 以及 24h Volume / Liquidity。

- `search-markets`
  - 入口：`poetry run poly-arb search-markets "<query>" --platform polymarket --limit 20`
  - 对 Polymarket：
    - 按标题关键字 / slug；
    - 按市场数值 ID；
    - 按 `conditionId` 前缀进行模糊匹配；
  - 对 Opinion：按标题关键字 / slug 匹配。

- `orderbook`
  - 先通过 `find_market_by_id` 在目标平台找到市场；
  - 调用客户端的 `get_orderbook(market, "yes"/"no")`；
  - 使用 `print_orderbook` 渲染 YES/NO 盘口。

- `price`
  - 同样通过 `find_market_by_id` 确定市场；
  - 调用 `client.get_best_prices(market)` 获取 YES/NO 最优价与流动性；
  - 用小表格展示单价与近端流动性。

---

## 4. 套利与对冲命令 `arb.py`

模块职责：将策略层的扫描能力包装成易用的 CLI。

内部辅助函数：

- `_scan(limit, threshold)`
  - 调用 `services.scanner.scan_once(...)`；
  - 结果用 `print_opportunities` 展示，并用 `storage.log_opportunities` 记录到 JSONL。

- `_scan_hedge(...)`
  - 使用 `load_hedge_markets` 读取对冲映射；
  - 调用 `scan_hedged_opportunities` 计算 Polymarket vs perp 的价格差；
  - 用 `print_hedge_opportunities` 展示，并输出日志。

- `_preview_matches(limit, threshold)`
  - 使用 `match_markets` 预览 Polymarket / Opinion 标题匹配效果，方便调参。

暴露的命令：

- `scan-arb`
  - 封装 `_scan`，参数为 `--limit` 与 `--threshold`；
  - 主要用于一次性扫描套利机会。

- `scan-hedge`
  - 封装 `_scan_hedge`；
  - 提供对冲映射路径、最小 edge%、默认波动率、perp 交易所与是否禁用 realized vol 等参数。

- `match-preview`
  - 调用 `_preview_matches`；
  - 适合在开发阶段观察 matcher 的表现。

- `run-bot`
  - 周期性调用 `scan_once`；
  - 支持 `--use-ws` 选项：
    - 若开启，则通过 `MarketWsFeed + PolymarketStreamState` 使用 WS 行情；
    - 否则退回 REST 盘口；
  - 使用 Rich Live 表格实时刷新套利机会列表。

---

## 5. 成交流水命令 `trades.py`

模块职责：提供「trades tape」风格的实时大额成交监控。

- 命令：`trades-tape`
  - 参数：
    - `--min-notional`：最小名义金额过滤（size * price）；
    - `--interval`：刷新间隔；
    - `--window`：界面中最多展示的成交条数。
  - 数据源：
    - 默认使用 `MarketWsFeed` 订阅 MARKET WebSocket；
    - 若 WS 状态中尚无成交，则自动退回 Data-API `/trades`。
  - 展示：
    - 上半部分：统计面板（Trade 数量 / 总成交额 / 平均单笔）；
    - 下半部分：详细流水表（时间、市场、方向、价格、成交量、Trader 等）。

---

## 6. 账户、TUI 与 Agent

### 6.1 账户命令 `account.py`

- 命令：`positions`
  - 通过各平台客户端的 `get_balances()` 获取余额/持仓；
  - 使用简单表格展示 Platform / Token / Balance；
  - Polymarket 侧目前为占位实现，Opinion 侧通过 CLOB SDK 实现只读查询。

### 6.2 TUI 与 Agent `tui_agent.py`

- 命令：`tui`
  - 启动 Textual 仪表盘（`ui.dashboard.run_dashboard`）；
  - 默认以 `scan_once` 为数据源，周期刷新套利机会。

- 命令：`agent`
  - 使用 `llm.agent.run_question` 封装的 LangChain 1.x Agent；
  - Agent 工具覆盖市场列表、盘口、套利扫描等接口。

---

## 7. 与领域服务层的边界

CLI 子包 **不直接实现策略逻辑或网络访问**，而是依赖：

- `clients.*`：完成 HTTP/WS 调用与数据解析；
- `services.*`：实现匹配、定价、套利/对冲扫描等「业务逻辑」；
- `types.*`：统一的数据模型。

这样设计的好处：

- CLI 可以在不触碰核心逻辑的前提下自由迭代（例如改用 Textual、增加新的视图）；
- 服务层可以被单元测试与其他上层（例如 Web API、Bot、Agent）复用；
- 将未来的扩展（新增平台、策略、数据源）限制在服务层和客户端层，减少对 CLI 的侵入式修改。

若需要进一步了解更底层的数据流与 WS 状态管理，可参考：

- `docs/architecture.md` 中的「数据接入层」「WebSocket Feed 与本地 State」「套利扫描器」章节。

