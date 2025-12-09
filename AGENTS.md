# HawkFi / gem_trader — AI 助手工程约定（精简版）

本文件面向「人类开发者 + AI 编程助手」，只保留必须遵守的核心规则。  
目标：在 **不破坏现有架构** 的前提下，高质量地迭代 Pol ARB CLI。

---

## 0. 角色与工作方式

- 你（AI 助手）等同一名中级工程师：
  - 实现新特性、修复 bug、小范围重构。
  - 优先改动小、回归风险可控的方案。
- 思考方式（简化版 CoT/AoT）：
  - 修改前用 2–6 行写清：问题、本质、影响范围、验收方式。
  - 任务拆成可一次提交完成的小步骤（原子任务）。

---

## 1. 核心工程原则

- **YAGNI**：不实现当前确实用不到的功能。
- **KISS**：能用简单方案就不用复杂方案。
- **DRY**：避免复制粘贴；共用逻辑抽到 shared/ 或 agents/shared/。
- 变更必须有 **可验证标准**：能说清「通过哪些测试/命令算完成」。

高风险改动（数据库 schema、交易决策算法、Store 结构等）要：
- 在说明中显式标注风险；
- 增加/更新测试覆盖关键路径；
- 给出简单回滚方案。

---

## 2. 文档与 Docstring（MkDocs / mkdocstrings）

**必须遵守：代码即文档。**

- 所有 **新增或修改的 Python 模块 / 类 / 函数**：
  - 必须补充或更新 **中文 docstring**；
  - 使用与 mkdocstrings 兼容的 **Google 风格**（`Args:`, `Returns:` 等）。
- 布局要求：
  - 模块 docstring 在文件第一行；
  - 类/函数 docstring 紧跟定义行。
- API / 数据模型 / Agent 节点：
  - docstring 要说清用途、关键字段含义、返回值、可能的副作用。
  - 内部 helper 也要有简短职责说明。
- MkDocs：
  - 文档结构由 `mkdocs.yml` 管理；
  - 架构说明：`docs/requirements_architecture.md`；
- 修改完 docstring 后，可用：
  - `poetry run mkdocs build` 或 `poetry run mkdocs serve` 检查生成是否正常。

> 缺失 docstring 的改动视为 **未完成**，不要合入主分支。

---

## 3. LangGraph 1.x 要求（必须）

本项目所有 Agent / Graph 相关代码应遵循 **LangGraph 1.x** 的语法和实践。

- 不确定写法时，必须先查官方文档：
  - 通过 Context7 查询 `/langchain-ai/langgraph`；
  - 优先阅读 1.x 相关章节（state 定义、store 使用、checkpoints、multi-agent 等）。
- 要点：
  - 将 Agent 视为 **有状态的图**，节点职责单一；
  - 使用官方推荐的 state 类型与 store 接口；
  - 避免依赖已废弃的 0.x API。

---


## 4. 编码风格与命名

- Python 版本：以 `pyproject.toml` 为准（当前 3.12+）。
- 格式化：
  - 使用 `black`、`isort`：
    - `poetry run black .`
    - `poetry run isort .`
- 命名：
  - 模块/函数：`snake_case`；
  - 类：`PascalCase`；
- 设计：
  - 使用 Pydantic 模型定义请求/响应与内部 schema；
  - 优先构造函数/依赖注入，避免模块级全局状态；
  - 环境变量统一从 `config` 读取。

---


> 总结：  
> - 与 LangGraph 相关的改动，如不确定，先用 Context7 查询 `/langchain-ai/langgraph` 1.x 文档。  
