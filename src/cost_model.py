"""
R156X UNIFIED COST MODEL (v2 engine companion)
===============================================
统一交易成本模型：maker/taker 分档、资金费（funding）、确认延迟（confirm latency）。

设计原则
--------
1. **纯 stdlib**：只依赖 ``dataclasses`` / ``math`` / ``typing``，可独立 import 与单测，
   不依赖 pandas / numpy / 网络。
2. **可审计**：所有成本按"单边名义价值 bps"表达，往返成本 = 入场成本 + 出场成本，
   引擎只扣一次；每一项（手续费/滑点/资金费）都能单独拆出来复核。
3. **档位即口径**：三个预置档位对应三种真实执行方式（maker 挂单、单venue taker、
   跨 venue 4 腿），历史结论必须在同一档位下比较，禁止混用。

成本口径约定
------------
- ``entry_bps``  / ``exit_bps``：单边（单侧）全部成本 = 手续费 + 滑点，单位 bps。
- ``roundtrip_bps``：一次完整开平仓的总成本 = ``entry_bps + exit_bps``。
- 四个数字均为"占名义价值"的 bps，与仓位权重无关（引擎最后乘 position_weight）。
- 资金费：正费率时多头支付、空头收取；默认 1.5 bps / 12h，按持有小时数比例扣减
  （``funding_mode='prorated'``，期望值口径）；也可用 ``funding_mode='events'``
  只对完整 12h 计费（更接近离散结算，偏保守），或传入真实费率序列。
- 延迟：``confirm_latency_seconds`` 默认 12s（与 R161B latency audit 记录的
  confirm ~12s 一致）。按 bar 粒度换算成"需要顺延的 bar 数"
  （``entry_delay_bars``），引擎据此把入场推迟到下一根可成交 bar 的 open。

预置档位
--------
==========================  ==========  ============  =============
档位                        入场 bps    出场 bps      往返 bps
==========================  ==========  ============  =============
MAKER_ROUNDTRIP_5BPS        2.5         2.5           5
SINGLE_VENUE_TAKER_14BPS    7.0         7.0           14
CROSS_VENUE_4LEG_26BPS      13.0        13.0          26
==========================  ==========  ============  =============

- MAKER：maker 手续费 2 bps/边 + 0.5 bps/边 挂单机会成本（排队/撤单/被扫），往返 5 bps。
- SINGLE_VENUE_TAKER：taker 手续费 5 bps/边 + 滑点 2 bps/边，往返 14 bps
  （散户单 venue taker 的常见真实成本）。
- CROSS_VENUE_4LEG：4 条腿（开仓 spot + perp，平仓 spot + perp），
  每条腿 taker 4.5 bps + 滑点 2 bps = 6.5 bps，单边 2 腿 13 bps，往返 26 bps。
- 冻结配置 ``r156a_v2_frozen.yaml`` 的 baseline 20 bps 属于"旧式单常数成本"，
  可用 ``CostModel.flat(20)`` 复现，但它没有 maker/taker、资金费、延迟分档。

使用示例
--------
>>> from cost_model import MAKER_ROUNDTRIP_5BPS, CostModel
>>> cm = MAKER_ROUNDTRIP_5BPS
>>> cm.roundtrip_bps
5.0
>>> cm.entry_delay_bars(interval_minutes=1)   # 12s 延迟 -> 顺延 1 根 1m bar
1
>>> cm.funding_cost_bps(hold_hours=24, direction='LONG')  # 1.5bps/12h * 24h
3.0
>>> cm.net_bps(gross_bps=50, direction='LONG', hold_hours=24)
42.0

本模块不修改任何 v1 文件；v1 引擎保持冻结，v2 引擎通过 ``cost_model`` 参数接入。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = [
    "CostModel",
    "MAKER_ROUNDTRIP_5BPS",
    "SINGLE_VENUE_TAKER_14BPS",
    "CROSS_VENUE_4LEG_26BPS",
    "PRESETS",
    "get_preset",
]

#: bps -> 小数收益的换算系数（1 bps = 1e-4）
BPS = 1e-4

#: 合法方向别名
_LONG_ALIASES = frozenset({"LONG", "BUY", "B"})
_SHORT_ALIASES = frozenset({"SHORT", "SELL", "S"})

#: 资金费口径
_FUNDING_MODES = frozenset({"prorated", "events"})


def _finite_non_negative(value: float, name: str) -> float:
    """把输入转成有限非负 float，否则抛 ValueError（便于在构造期 fail fast）。"""
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御式
        raise ValueError(f"{name} must be a number, got {value!r}") from exc
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite, got {out!r}")
    if out < 0:
        raise ValueError(f"{name} must be non-negative, got {out!r}")
    return out


def _normalize_direction(direction: str) -> str:
    """方向归一化为 'LONG' / 'SHORT'，非法值抛 ValueError。"""
    if not isinstance(direction, str):
        raise ValueError(f"direction must be a string, got {direction!r}")
    upper = direction.strip().upper()
    if upper in _LONG_ALIASES:
        return "LONG"
    if upper in _SHORT_ALIASES:
        return "SHORT"
    raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")


@dataclass(frozen=True)
class CostModel:
    """单笔交易成本模型（手续费 + 滑点 + 资金费 + 确认延迟）。

    Parameters
    ----------
    name:
        档位名称，写进交易记录的 ``cost_model`` 字段，便于回溯口径。
    entry_fee_bps / exit_fee_bps:
        单边手续费（bps）。跨 venue 多腿时这里填"该侧所有腿之和"。
    entry_slippage_bps / exit_slippage_bps:
        单边滑点/冲击成本（bps）。冻结 config 里 slippage 明确并入成本，故单独列出以便复核。
    funding_bps_per_12h:
        默认资金费率，单位 bps / 12h。正数表示多头支付。默认 1.5。
    funding_interval_hours:
        资金费结算周期，默认 12h（OKX/主流永续）。
    funding_mode:
        ``'prorated'``（默认，期望值口径：按 hold_hours/12h 比例扣）
        或 ``'events'``（只对完整 12h 整数倍扣费，离散结算偏保守）。
    confirm_latency_seconds:
        信号确认延迟（秒），默认 12.0，对应 R161B latency audit 的 confirm ~12s。
    interval_minutes:
        默认 bar 周期（分钟），供 ``entry_delay_bars()`` 无参调用时使用。
    legs_per_side:
        单边腿数（仅元数据，便于报告说明；不参与算术）。
        单 venue/单腿 = 1，跨 venue（spot+perp）= 2。
    """

    name: str = "CUSTOM"
    entry_fee_bps: float = 0.0
    exit_fee_bps: float = 0.0
    entry_slippage_bps: float = 0.0
    exit_slippage_bps: float = 0.0
    funding_bps_per_12h: float = 1.5
    funding_interval_hours: float = 12.0
    funding_mode: str = "prorated"
    confirm_latency_seconds: float = 12.0
    interval_minutes: float = 1.0
    legs_per_side: int = 1

    # ------------------------------------------------------------------ 校验
    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_fee_bps", _finite_non_negative(self.entry_fee_bps, "entry_fee_bps"))
        object.__setattr__(self, "exit_fee_bps", _finite_non_negative(self.exit_fee_bps, "exit_fee_bps"))
        object.__setattr__(
            self, "entry_slippage_bps", _finite_non_negative(self.entry_slippage_bps, "entry_slippage_bps")
        )
        object.__setattr__(
            self, "exit_slippage_bps", _finite_non_negative(self.exit_slippage_bps, "exit_slippage_bps")
        )
        object.__setattr__(
            self, "confirm_latency_seconds", _finite_non_negative(self.confirm_latency_seconds, "confirm_latency_seconds")
        )
        object.__setattr__(self, "interval_minutes", _finite_non_negative(self.interval_minutes, "interval_minutes"))
        try:
            interval_h = float(self.funding_interval_hours)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 防御式
            raise ValueError("funding_interval_hours must be a number") from exc
        if not math.isfinite(interval_h) or interval_h <= 0:
            raise ValueError(f"funding_interval_hours must be positive finite, got {self.funding_interval_hours!r}")
        object.__setattr__(self, "funding_interval_hours", interval_h)
        try:
            funding_rate = float(self.funding_bps_per_12h)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 防御式
            raise ValueError("funding_bps_per_12h must be a number") from exc
        if not math.isfinite(funding_rate):
            raise ValueError(f"funding_bps_per_12h must be finite, got {self.funding_bps_per_12h!r}")
        object.__setattr__(self, "funding_bps_per_12h", funding_rate)
        mode = str(self.funding_mode).lower()
        if mode not in _FUNDING_MODES:
            raise ValueError(f"funding_mode must be one of {sorted(_FUNDING_MODES)}, got {self.funding_mode!r}")
        object.__setattr__(self, "funding_mode", mode)
        legs = int(self.legs_per_side)
        if legs < 1:
            raise ValueError(f"legs_per_side must be >= 1, got {self.legs_per_side!r}")
        object.__setattr__(self, "legs_per_side", legs)

    # -------------------------------------------------------------- 成本属性
    @property
    def entry_bps(self) -> float:
        """单边入场总成本（手续费 + 滑点，bps）。"""
        return self.entry_fee_bps + self.entry_slippage_bps

    @property
    def exit_bps(self) -> float:
        """单边出场总成本（手续费 + 滑点，bps）。"""
        return self.exit_fee_bps + self.exit_slippage_bps

    @property
    def roundtrip_bps(self) -> float:
        """一次完整往返的总成本（bps）。引擎每笔交易只扣这一次。"""
        return self.entry_bps + self.exit_bps

    @property
    def funding_bps_per_hour(self) -> float:
        """默认资金费折算到每小时的 bps（不区分方向）。"""
        return self.funding_bps_per_12h / self.funding_interval_hours

    # ------------------------------------------------------------ 延迟（bar）
    def entry_delay_bars(self, interval_minutes: float | None = None) -> int:
        """确认延迟折算成需要顺延的 bar 数（bar 粒度、保守取整）。

        12s 延迟在 1m bar 上 = 顺延 1 根 bar；在 <12s 的 bar 上按
        ``ceil(latency / bar_seconds)`` 顺延更多根。原因：决策可能发生在 bar 内任意
        时刻，<1 个 bar 的延迟仍可能错过下一根 bar 的 open，故只要延迟 > 0，
        至少顺延 1 根（最坏情况保证，无未来函数）。

        Parameters
        ----------
        interval_minutes:
            bar 周期（分钟）。``None`` 时用构造参数 ``interval_minutes``。

        Returns
        -------
        int
            需要顺延的 bar 数（0 表示无延迟）。
        """
        bar_min = self.interval_minutes if interval_minutes is None else float(interval_minutes)
        if not math.isfinite(bar_min) or bar_min <= 0:
            raise ValueError(f"interval_minutes must be positive finite, got {bar_min!r}")
        if self.confirm_latency_seconds <= 0:
            return 0
        bar_seconds = bar_min * 60.0
        return max(1, int(math.ceil(self.confirm_latency_seconds / bar_seconds - 1e-12)))

    # ------------------------------------------------------------ 资金费
    def _funding_rate_per_12h(self, rates_bps: Sequence[float] | None) -> float:
        """把默认费率或真实费率序列归一成"等效 12h 费率"。"""
        if rates_bps is None:
            return self.funding_bps_per_12h
        values = []
        for i, rate in enumerate(rates_bps):
            try:
                val = float(rate)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"funding rate[{i}] is not a number: {rate!r}") from exc
            if not math.isfinite(val):
                raise ValueError(f"funding rate[{i}] must be finite, got {rate!r}")
            values.append(val)
        if not values:
            raise ValueError("rates_bps must be non-empty when provided")
        # 真实费率序列 -> 期间等效 12h 费率（均值），再按持有小时数比例折算。
        return sum(values) / len(values)

    def funding_rate_sum_bps(
        self,
        hold_hours: float,
        rates_bps: Sequence[float] | None = None,
    ) -> float:
        """持有期间累计资金费率（无符号，bps；正数=多头支付）。

        - ``funding_mode='prorated'``：``等效12h费率 * hold_hours / 12``（期望值口径）。
        - ``funding_mode='events'``：只对完整 12h 整数倍计费；若给了费率序列，
          依次取前 ``n = floor(hold_hours / 12)`` 个事件求和（序列不足时循环取用）。
        """
        try:
            hours = float(hold_hours)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 防御式
            raise ValueError(f"hold_hours must be a number, got {hold_hours!r}") from exc
        if not math.isfinite(hours):
            raise ValueError(f"hold_hours must be finite, got {hold_hours!r}")
        if hours < 0:
            raise ValueError(f"hold_hours must be non-negative, got {hours!r}")
        interval = self.funding_interval_hours
        if self.funding_mode == "events":
            n_events = int(math.floor(hours / interval + 1e-12))
            if n_events <= 0:
                return 0.0
            if rates_bps is None:
                return self.funding_bps_per_12h * n_events
            seq = [float(r) for r in rates_bps]
            if not seq:
                raise ValueError("rates_bps must be non-empty when provided")
            return float(sum(seq[i % len(seq)] for i in range(n_events)))
        # prorated（默认）
        rate = self._funding_rate_per_12h(rates_bps)
        return rate * hours / interval

    def funding_cost_bps(
        self,
        hold_hours: float,
        direction: str = "LONG",
        rates_bps: Sequence[float] | None = None,
    ) -> float:
        """把资金费换算成"应从 gross PnL 中扣减的成本"（带符号，bps）。

        正资金费率下：LONG 支付（返回正数=成本），SHORT 收取（返回负数=收益）。
        负费率反之。引擎使用 ``net = gross - entry - exit - funding_cost``。
        """
        rate_sum = self.funding_rate_sum_bps(hold_hours, rates_bps=rates_bps)
        side = _normalize_direction(direction)
        return rate_sum if side == "LONG" else -rate_sum

    # ------------------------------------------------------------ 净收益
    def net_bps(
        self,
        gross_bps: float,
        direction: str = "LONG",
        hold_hours: float = 0.0,
        rates_bps: Sequence[float] | None = None,
    ) -> float:
        """净收益（bps）= 毛利 - 往返成本 - 资金费成本。"""
        gross = float(gross_bps)
        return gross - self.roundtrip_bps - self.funding_cost_bps(
            hold_hours, direction=direction, rates_bps=rates_bps
        )

    # ------------------------------------------------------------ 工具方法
    def as_dict(self) -> dict[str, float | str | int]:
        """导出为可 JSON 序列化的 dict（写进报告 / summary 用）。"""
        return {
            "name": self.name,
            "entry_fee_bps": self.entry_fee_bps,
            "exit_fee_bps": self.exit_fee_bps,
            "entry_slippage_bps": self.entry_slippage_bps,
            "exit_slippage_bps": self.exit_slippage_bps,
            "entry_bps": self.entry_bps,
            "exit_bps": self.exit_bps,
            "roundtrip_bps": self.roundtrip_bps,
            "funding_bps_per_12h": self.funding_bps_per_12h,
            "funding_interval_hours": self.funding_interval_hours,
            "funding_mode": self.funding_mode,
            "confirm_latency_seconds": self.confirm_latency_seconds,
            "interval_minutes": self.interval_minutes,
            "legs_per_side": self.legs_per_side,
        }

    def with_latency(self, confirm_latency_seconds: float) -> "CostModel":
        """返回同口径但延迟不同的副本（用于延迟敏感性测试）。"""
        return CostModel(
            name=self.name,
            entry_fee_bps=self.entry_fee_bps,
            exit_fee_bps=self.exit_fee_bps,
            entry_slippage_bps=self.entry_slippage_bps,
            exit_slippage_bps=self.exit_slippage_bps,
            funding_bps_per_12h=self.funding_bps_per_12h,
            funding_interval_hours=self.funding_interval_hours,
            funding_mode=self.funding_mode,
            confirm_latency_seconds=confirm_latency_seconds,
            interval_minutes=self.interval_minutes,
            legs_per_side=self.legs_per_side,
        )

    def with_funding(self, funding_bps_per_12h: float) -> "CostModel":
        """返回同交易成本但资金费不同的副本（用于资金费敏感性测试）。"""
        return CostModel(
            name=self.name,
            entry_fee_bps=self.entry_fee_bps,
            exit_fee_bps=self.exit_fee_bps,
            entry_slippage_bps=self.entry_slippage_bps,
            exit_slippage_bps=self.exit_slippage_bps,
            funding_bps_per_12h=funding_bps_per_12h,
            funding_interval_hours=self.funding_interval_hours,
            funding_mode=self.funding_mode,
            confirm_latency_seconds=self.confirm_latency_seconds,
            interval_minutes=self.interval_minutes,
            legs_per_side=self.legs_per_side,
        )

    @classmethod
    def flat(
        cls,
        roundtrip_bps: float,
        name: str = "FLAT",
        funding_bps_per_12h: float = 0.0,
        confirm_latency_seconds: float = 0.0,
    ) -> "CostModel":
        """旧式单常数成本模型的等价表达（复现 v1 ``cost_bps`` 语义用）。

        往返成本平均拆到入场/出场两侧，``roundtrip_bps`` 与 v1 的 ``cost_bps`` 完全一致。
        默认无资金费、无延迟，以便与 v1 结果逐位对齐。
        """
        total = _finite_non_negative(roundtrip_bps, "roundtrip_bps")
        return cls(
            name=name,
            entry_fee_bps=total / 2.0,
            exit_fee_bps=total / 2.0,
            funding_bps_per_12h=funding_bps_per_12h,
            confirm_latency_seconds=confirm_latency_seconds,
            interval_minutes=1.0,
        )

    @classmethod
    def from_preset(cls, name: str) -> "CostModel":
        """按档位名取预置模型（大小写不敏感），见 ``PRESETS``。"""
        return get_preset(name)


# ---------------------------------------------------------------------------
# 三个预置档位（模块级常量，可直接 import；也可 by-name 从 PRESETS 取）
# ---------------------------------------------------------------------------
#: maker 挂单：手续费 2 bps/边 + 0.5 bps/边 排队/被扫机会成本 = 往返 5 bps
MAKER_ROUNDTRIP_5BPS = CostModel(
    name="MAKER_ROUNDTRIP_5BPS",
    entry_fee_bps=2.0,
    exit_fee_bps=2.0,
    entry_slippage_bps=0.5,
    exit_slippage_bps=0.5,
    funding_bps_per_12h=1.5,
    confirm_latency_seconds=12.0,
    interval_minutes=1.0,
    legs_per_side=1,
)

#: 单 venue taker：手续费 5 bps/边 + 滑点 2 bps/边 = 往返 14 bps
SINGLE_VENUE_TAKER_14BPS = CostModel(
    name="SINGLE_VENUE_TAKER_14BPS",
    entry_fee_bps=5.0,
    exit_fee_bps=5.0,
    entry_slippage_bps=2.0,
    exit_slippage_bps=2.0,
    funding_bps_per_12h=1.5,
    confirm_latency_seconds=12.0,
    interval_minutes=1.0,
    legs_per_side=1,
)

#: 跨 venue 4 腿（开 spot+perp，平 spot+perp）：每腿 taker 4.5 + 滑点 2 = 6.5 bps，
#: 单边 2 腿 13 bps，往返 26 bps
CROSS_VENUE_4LEG_26BPS = CostModel(
    name="CROSS_VENUE_4LEG_26BPS",
    entry_fee_bps=9.0,        # 2 腿 * 4.5 bps taker
    exit_fee_bps=9.0,
    entry_slippage_bps=4.0,   # 2 腿 * 2.0 bps 滑点
    exit_slippage_bps=4.0,
    funding_bps_per_12h=1.5,
    confirm_latency_seconds=12.0,
    interval_minutes=1.0,
    legs_per_side=2,
)

#: 档位注册表，name -> CostModel
PRESETS: Mapping[str, CostModel] = {
    "MAKER_ROUNDTRIP_5BPS": MAKER_ROUNDTRIP_5BPS,
    "SINGLE_VENUE_TAKER_14BPS": SINGLE_VENUE_TAKER_14BPS,
    "CROSS_VENUE_4LEG_26BPS": CROSS_VENUE_4LEG_26BPS,
}


def get_preset(name: str) -> CostModel:
    """按名称取预置档位；未知名称抛 KeyError 并列出合法档位。"""
    key = str(name).strip().upper()
    if key not in PRESETS:
        raise KeyError(f"unknown cost preset {name!r}; available: {sorted(PRESETS)}")
    return PRESETS[key]
