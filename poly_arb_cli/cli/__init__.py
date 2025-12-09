"""Polymarket–Opinion CLI 顶层入口。

本模块仅负责定义 Click 命令组并导入各子命令模块，
实际业务逻辑拆分在 `poly_arb_cli.cli.*` 子包中。
"""

from __future__ import annotations

import click


@click.group()
def main() -> None:
    """Polymarket-Opinion arbitrage CLI."""


# 导入子模块以注册子命令（装饰器在导入时执行）
from . import account as _account  # noqa: F401,E402
from . import arb as _arb  # noqa: F401,E402
from . import markets as _markets  # noqa: F401,E402
from . import tags as _tags  # noqa: F401,E402
from . import trades as _trades  # noqa: F401,E402
from . import tui_agent as _tui_agent  # noqa: F401,E402


__all__ = ["main"]
