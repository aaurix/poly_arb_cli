"""触及型期权概率近似工具。

参考触及概率反射原理：GBM 假设下 one-touch 概率可用封闭式近似。
此处用于评估 Polymarket “是否触及/超过阈值”类事件的隐含概率。
"""

from __future__ import annotations

import math
from typing import Optional


def one_touch_prob(
    spot: float,
    barrier: float,
    years: float,
    vol: float,
    drift: float = 0.0,
    direction: str = "up",
) -> Optional[float]:
    """估计 one-touch 触及概率（漂移近似）。

    Args:
        spot: 当前标的价格。
        barrier: 触及阈值。
        years: 剩余到期时间（年）。
        vol: 年化波动率（>=0）。
        drift: 年化漂移（风险中性下通常为 0）。
        direction: “up” 表示上破，“down” 表示下破。

    Returns:
        触及概率，若输入无效返回 None。
    """
    if spot <= 0 or barrier <= 0 or years <= 0 or vol <= 0:
        return None

    sigma = vol
    sqrt_t = math.sqrt(years)
    if direction == "down":
        # 下破：转换为上破的等价形式
        gap = math.log(spot / barrier)
        # 漂移向下则更易触及，向上则降低概率
        drift_term = -drift * years
    else:
        gap = math.log(barrier / spot)
        drift_term = drift * years

    if gap <= 0:
        return 1.0

    z = (gap - drift_term) / (sigma * sqrt_t)
    # 反射原理的简化版，忽略高阶项
    prob = 2 * (1 - norm_cdf(z))
    return max(0.0, min(1.0, prob))


def no_touch_prob(
    spot: float,
    barrier: float,
    years: float,
    vol: float,
    drift: float = 0.0,
    direction: str = "up",
) -> Optional[float]:
    """估计 no-touch 概率（未触及）。"""
    touch = one_touch_prob(spot, barrier, years, vol, drift=drift, direction=direction)
    return None if touch is None else max(0.0, 1.0 - touch)


def norm_cdf(x: float) -> float:
    """正态分布累积函数。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
