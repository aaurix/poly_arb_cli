# Polymarket-Opinion CLI & TUI（套利监控与交易脚手架）

基于 Polymarket Gamma/CLOB + Opinion CLOB SDK 的 Python CLI/TUI，用于市场监控、交叉套利扫描、下单协调，并支持 LangChain 1.x agent 工具扩展。

## 快速开始
- Python 3.10+，建议 `python -m venv .venv && source .venv/bin/activate`
- 安装依赖：`poetry install`（联网后执行；包含 opinion-clob-sdk==0.4.3、py-clob-client、LangChain >=1.x）
- 配置 `.env` 后直接使用线上实时数据：  
  - `poetry run poly-arb list-markets --platform all|polymarket|opinion`  
  - `poetry run poly-arb scan-arb`  
  - `poetry run poly-arb scan-hedge --map-path data/underlying_map.json`（需 ccxt，映射示例见 `data/underlying_map.sample.json`）  
  - `poetry run poly-arb run-bot --interval 10`  
  - `poetry run poly-arb orderbook <market_id> --platform polymarket`  
  - `poetry run poly-arb positions --platform all`  
  - `poetry run poly-arb tui`（Textual 仪表盘）  
  - `poetry run poly-arb agent "list top markets"`  

## 配置（.env 示例）
```
POLYMKT_CLOB_API_KEY=
POLYGON_WALLET_PRIVATE_KEY=
POLYMARKET_BASE_URL=https://gamma-api.polymarket.com
POLYMARKET_CLOB_URL=https://clob.polymarket.com
POLYMARKET_WS_URL=wss://clob.polymarket.com/stream
OPINION_API_KEY=
OPINION_PRIVATE_KEY=
OPINION_HOST=https://proxy.opinion.trade:8443
OPINION_WS_URL=wss://proxy.opinion.trade:8443/ws
RPC_URL=
SCAN_INTERVAL_SECONDS=60
MAX_TRADE_SIZE=50
MIN_TRADE_SIZE=5
DEFAULT_QUOTE_SIZE=10
MAX_SLIPPAGE_BPS=150
MIN_PROFIT_PERCENT=1.0
DEMO_MODE=true
```

## 目录与核心模块
- `pyproject.toml`：依赖与入口 `poly-arb`
- `poly_arb_cli/config.py`：配置加载（pydantic-settings）
- `poly_arb_cli/types.py`：市场/订单簿/套利/交易结果数据模型
- `poly_arb_cli/clients/`：Polymarket & Opinion 客户端（市场、订单簿、下单、余额/撤单）
- `poly_arb_cli/services/`：matcher、pricing、scanner、trader（深度/滑点计算、并发下单骨架）
- `poly_arb_cli/ui/`：Typer CLI、Textual 仪表盘
- `poly_arb_cli/connectors/`：新闻/搜索/向量库占位（RAG 可扩展）
- `poly_arb_cli/llm/`：LangChain v1 agent/tools（市场/订单簿工具，`agent` 命令）
- `poly_arb_cli/storage.py`：JSONL 持久化（机会、交易）

## 工作流（目标形态）
1) 拉取双边市场 → 标题/到期日匹配  
2) 获取订单簿并按深度计算均价，判定套利（PM_NO+OP_YES / PM_YES+OP_NO < 1，含滑点/最小成交量）  
3) 排序机会，调用 trader 并发下单（后续加撤单/对冲）  
4) 展示与记录：Rich 表格、Textual 仪表盘、JSONL 日志  
5) LangChain agent 可调用内置工具查询市场/订单簿，后续可扩展检索/摘要

## 当前进度与待办
- 已有：深度扫描、滑点控制、positions、orderbook、TUI、JSONL 日志、LangChain 工具/agent 命令。
- 待补：实盘 API 校验（Gamma/Opinion 字段、下单/撤单）、真实回滚/对冲、PNL/持仓明细、告警/更丰富持久化、RAG 数据源接入。

## 测试
- 联网后运行：`poetry run ruff check .`、`poetry run mypy .`、`pytest`（建议补充单测：matcher/scanner/pricing/trader）。
# poly_arb_cli
