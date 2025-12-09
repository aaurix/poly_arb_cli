# Polymarket–Opinion CLI 集成 Polymarket Agents 功能需求与架构规划

本文件在现有 `poly-arb-cli` 脚手架基础上，结合官方 SDK（`opinion-clob-sdk==0.4.3`、`py-clob-client`）、Polymarket Agents 公开文档与模块划分，梳理功能需求、架构设计与落地路线。LangChain 集成遵循 1.x 语法（LCEL）。

## 目标与范围
- 复用/对齐 Polymarket Agents 能力：市场数据拉取、订单构建与签名、下单执行、行情与深度查询。
- 支持 Opinion 与 Polymarket 双边套利扫描与执行（核心条件：`PM_NO + OP_YES < 1` 或 `PM_YES + OP_NO < 1`）。
- 提供 CLI/TUI：行情列表、匹配预览、套利扫描、自动监控、持仓与订单簿查看。
- 支持可选情报/RAG：新闻抓取、搜索、向量检索，辅助策略（参考 Agents 的 `connectors/chroma.py`、`news.py`、`search.py`），使用 LangChain 1.x。
- 安全与可靠性：配置管理、密钥隔离、速率限制、重试、日志和审计。

## 功能需求
1) 数据源与模型
   - Polymarket Gamma API：活跃市场/事件列表、订单簿（最佳报价/深度）。
   - Polymarket CLOB：下单、撤单、余额/持仓（py-clob-client）。
   - Opinion Open API/CLOB SDK：市场列表、订单簿、下单、余额/持仓。
   - 统一数据模型（Pydantic）：Market/Event、Token、OrderBook、Quote、MatchPair、ArbOpportunity、Position、TradeLog。

2) 匹配与套利
   - 市场匹配：标题相似度 + 到期日/类别；支持人工映射表与阈值调整。
   - 报价聚合：按订单簿最优价或中间价；可配置深度/滑点容忍度。
   - 套利判定：双条件计算成本与收益率，结合可成交量（基于深度）。
   - 结果排序与过滤：按收益率、流动性、最小规模过滤。

3) 交易执行
   - 原子性：并发双边下单；失败时回滚/补单策略。
   - 价格保护：滑点上限、最大成交额、最小成交额。
   - 重试与超时：tenacity 配置重试策略；错误分类（可重试/不可重试）。
   - 记录：订单请求/响应、成交结果、失败原因，输出 JSON/CSV。

4) CLI/TUI
   - `list-markets [polymarket|opinion|all]`
   - `match-preview`：显示匹配对及相似度。
   - `scan-arb`：单次扫描输出套利机会。
   - `run-bot`：周期扫描 + 自动/半自动执行（后续实现）。
   - `positions`：查询双边余额/持仓。
   - `orderbook <market_id>`：展示深度与估算可成交量。
   - Textual 版多栏视图（行情/深度/价差）作为可选 TUI。

5) 情报/RAG（可选但对齐 Agents）
   - 新闻/搜索拉取（等价于 `connectors/news.py`、`search.py`）；
   - 向量库（Chroma 或可替换）：存储市场/事件背景；LCEL 检索链。
   - 提示模板与工具：LangChain Runnable 组合，支持查询→检索→总结。

6) 配置与安全
   - `.env`/环境变量管理：API Key、私钥、RPC、速率、滑点、扫描频率。
   - 不将密钥写入日志；生产模式默认关闭 demo。
   - 速率限制与节流：Gamma/Open API 调用间隔可配置。

7) 质量与测试
   - 单元测试：匹配、套利计算、深度估算、容错。
   - 集成测试：在测试网/沙盒验证下单与回滚。
   - 类型/静态检查：mypy、ruff。

## 架构与模块映射
- 配置层：`poly_arb_cli/config.py`（env + defaults）。
- 数据模型：`poly_arb_cli/types.py`（需扩展：OrderBook, Position, TradeLog）。
- 连接器
  - Polymarket：`polymarket_client.py`（Gamma + py-clob-client）；补齐订单簿解析、余额/持仓接口。
  - Opinion：`opinion_client.py`（opinion-clob-sdk 0.4.3）；补齐 Open API 读写、余额/持仓。
  - 情报（新）：`connectors/news.py`, `connectors/search.py`, `connectors/vector.py`（Chroma/FAISS），LangChain Runnables。
- 服务层
  - 匹配：`matcher.py`（改进相似度、人工映射表支持）。
  - 报价聚合：`pricing.py`（新，含深度/滑点计算）。
  - 扫描：`scanner.py`（接入深度、收益/容量计算）。
  - 交易：`trader.py`（并发下单、回滚、重试）。
  - 日志/审计：`logging.py`（新，结构化日志与持久化）。
  - 策略编排：`orchestrator.py`（新，调度扫描→评估→交易）。
- 接口层
  - CLI/TUI：`cli.py`（Typer）；Textual 视图（可在 `tui/`）。
  - API（可选）：FastAPI 轻量接口供前端或自动化调用。

## 数据流
1. 启动 CLI：加载配置 → 构建客户端（真实/ demo）。
2. 定时任务：拉取市场列表 → 匹配 → 获取订单簿报价与深度。
3. 计算套利：判定路线（PM_NO+OP_YES、PM_YES+OP_NO），估算收益率与最大可成交量。
4. 过滤排序：按收益/流动性/阈值输出机会。
5. 执行策略：并发双边下单 → 监控回执 → 失败回滚/重试。
6. 记录：结构化日志、CLI/TUI 展示、可选持久化。
7. （可选）情报：市场上下文→检索→提示生成，辅助决策或提醒。

## 实施路线（里程碑，当前进度）
M1. SDK 接入与基础数据
   - 安装并验证 `opinion-clob-sdk==0.4.3`、`py-clob-client`。
   - 完成 Polymarket Gamma 市场/订单簿解析；完成 Opinion 市场/订单簿获取。
   - 扩展数据模型与配置项。

M2. 扫描与深度
   - 在 `scanner.py` 中加入深度与最大成交量估算；新建 `pricing.py`。
   - 可配置滑点、最小/最大额；匹配阈值与人工映射支持。

M3. 交易执行与回滚
   - `trader.py` 支持并发下单、重试、失败补救（撤单/对冲）。→ 已有并发+重试框架，需补充真实撤单/对冲。
   - 结构化日志与交易记录落盘。→ JSONL 已实现，需扩展告警/持久化。

M4. CLI/TUI 完善
   - 完成 `positions`/`orderbook` 实现；`run-bot` 支持自动模式。→ positions/orderbook 已可用，需实盘验证。
   - Textual 界面原型（分栏行情/深度/价差）。→ 已提供 `tui`，可优化布局。

M5. 情报与 RAG（可选）
   - 接入新闻/搜索、向量库；LangChain 1.x 检索链与摘要命令。

M6. 测试与发布
   - 单元/集成测试，CI 配置（ruff+mypy+pytest）；文档更新。

## 模块任务清单（短期）
- `polymarket_client.py`：补齐 Gamma 解析、CLOB 下单/撤单/余额、订单簿归一化。
- `opinion_client.py`：完善市场字段映射、订单簿深度解析、余额/持仓获取。
- `types.py`：增加 OrderBookLevel, OrderBook, Position, TradeResult。
- `scanner.py`：使用归一化订单簿，估算可成交量，返回 size、盈亏。
- `trader.py`：引入并发、回滚策略、tenacity 重试。
- `cli.py`：实盘开关、风险参数（滑点/限额）、positions/orderbook 子命令。
- `logging`/`storage`（新）：结构化日志 + CSV/JSON 记录。
- `tui`（新）：Textual 视图。
- `connectors`（新）：新闻/搜索/向量库 + LangChain 1.x Runnables。

## LangChain 1.x 集成提示（结合 v1 发布文档最佳实践）
- 使用 `langchain`, `langchain-community`, `langchain-openai` >=1.x；遗留功能才使用 `langchain-classic`。
- 构建代理时优先 `create_agent` + 中间件（PII/HITL/摘要等），工具接口使用官方 Tool；需要结构化输出时用 `ToolStrategy(PydanticModel)`。
- 统一采用 LCEL Runnable (`.invoke/.ainvoke/.stream`) 替代旧链式接口，确保同步/异步、流式一致。
- 中间件原则：单一职责、错误隔离、执行顺序明确、记录自定义 state，先单测再集成。
- 内容块统一访问：通过 `response.content_blocks` 解析 reasoning/text/tool_call，减少不同模型提供方的差异。
- 生产建议：速率/重试/缓存中间件，监控 token 与延迟；先静态提示+少量工具，逐步增加检索与动态上下文。

## 风险与注意事项
- 合规：遵守 Polymarket/Opinion TOS；部分地区限制交易。
- 速率与配额：Gamma/Open API 速率限制；需节流与缓存。
- 密钥安全：严格使用环境变量，日志脱敏。
- 同步下单风险：确保超时、回滚与对冲策略，避免单边成交。
