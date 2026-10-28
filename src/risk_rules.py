#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""严格风控规则（实盘前必须全部通过）。

设计原则：
- 纯函数 + 不可变配置，无网络依赖，可单元测试；
- 所有检查返回"拒绝原因列表"（空列表 = 通过），便于日志与审计；
- 规则写死在代码里，不因行情/情绪临时修改；改规则必须改版本号并记录。

用法：
    from risk_rules import RiskConfig, check_entry, check_portfolio, drawdown_pct
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

VERSION = "1.0.0"


@dataclass(frozen=True)
class RiskConfig:
    """实盘风控参数（默认值即上线规则）。"""

    capital_usd: float
    max_total_leverage: float = 1.0      # 总名义 / 权益
    max_asset_share: float = 0.50        # 单资产 sleeve / 权益
    max_drawdown_pct: float = 5.0        # 组合回撤达到即停止
    max_daily_loss_pct: float = 2.0      # 单日亏损达到即停止
    funding_negative_halt: int = 12      # 连续负资金费结算数达到即平仓（12 ≈ 半天，经历史验证；3 过于敏感）
    max_basis_bps: float = 50.0          # 入场时 |basis| 上限
    max_funding_abs_bps: float = 50.0    # 单次资金费绝对值上限（异常检测）
    capacity_frac: float = 0.01          # 单资产名义 ≤ 日现货成交额 × 该比例
    min_order_usd: float = 10.0          # 交易所最小下单金额
    version: str = VERSION

    def __post_init__(self) -> None:
        if not (0 < self.capital_usd <= 1e9):
            raise ValueError("capital_usd 必须在 (0, 1e9]")
        if not (0 < self.max_total_leverage <= 3):
            raise ValueError("max_total_leverage 必须在 (0, 3]")
        if not (0 < self.max_asset_share <= 1):
            raise ValueError("max_asset_share 必须在 (0, 1]")
        for name in ("max_drawdown_pct", "max_daily_loss_pct", "max_basis_bps",
                     "max_funding_abs_bps", "capacity_frac", "min_order_usd"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须为正数")
        if self.funding_negative_halt < 1:
            raise ValueError("funding_negative_halt 必须 >= 1")


@dataclass(frozen=True)
class Sizing:
    sleeve_usd: float
    notional_usd: float
    perp_margin_usd: float


def size_position(capital_usd: float, n_assets: int, max_lev: int, cfg: RiskConfig) -> Sizing:
    """等权 sleeve + 1x delta-neutral 名义计算。"""
    if n_assets < 1:
        raise ValueError("n_assets 必须 >= 1")
    sleeve = capital_usd / n_assets
    sleeve = min(sleeve, capital_usd * cfg.max_asset_share)
    notional = sleeve / (1 + 1 / max_lev)
    return Sizing(
        sleeve_usd=round(sleeve, 2),
        notional_usd=round(notional, 2),
        perp_margin_usd=round(notional / max_lev, 2),
    )


def check_entry(
    symbol: str,
    notional_usd: float,
    spot_mid: float,
    perp_mid: float,
    funding_rate: float,
    spot_day_vol_usd: float,
    cfg: RiskConfig,
) -> list[str]:
    """入场前检查：返回拒绝原因（空 = 允许入场）。"""
    reasons: list[str] = []
    if notional_usd < cfg.min_order_usd:
        reasons.append(f"{symbol}: 名义 ${notional_usd:.2f} < 最小下单 ${cfg.min_order_usd:.0f}")
    if spot_mid <= 0 or perp_mid <= 0:
        reasons.append(f"{symbol}: 盘口价格无效 spot={spot_mid} perp={perp_mid}")
        return reasons
    basis_bps = (perp_mid - spot_mid) / spot_mid * 1e4
    if abs(basis_bps) > cfg.max_basis_bps:
        reasons.append(f"{symbol}: |basis|={basis_bps:.2f}bps > {cfg.max_basis_bps}bps")
    if abs(funding_rate) * 1e4 > cfg.max_funding_abs_bps:
        reasons.append(
            f"{symbol}: |funding|={abs(funding_rate)*1e4:.2f}bps/h > {cfg.max_funding_abs_bps}bps/h")
    if spot_day_vol_usd > 0 and notional_usd > spot_day_vol_usd * cfg.capacity_frac:
        reasons.append(
            f"{symbol}: 名义 ${notional_usd:.0f} > 日现货成交额 {cfg.capacity_frac:.1%} "
            f"(${spot_day_vol_usd*cfg.capacity_frac:,.0f})")
    return reasons


def check_portfolio(
    positions: Sequence[dict],
    capital_usd: float,
    cfg: RiskConfig,
) -> list[str]:
    """组合层检查。positions: [{symbol, notional_usd}, ...]"""
    reasons: list[str] = []
    total_notional = sum(p["notional_usd"] for p in positions)
    if total_notional > capital_usd * cfg.max_total_leverage:
        reasons.append(
            f"总名义 ${total_notional:,.2f} > 权益 ${capital_usd:,.2f} × {cfg.max_total_leverage:.1f}")
    for p in positions:
        share = p["notional_usd"] / capital_usd if capital_usd > 0 else 0
        if share > cfg.max_asset_share:
            reasons.append(
                f"{p['symbol']}: 单资产名义占比 {share:.1%} > {cfg.max_asset_share:.0%}")
    return reasons


def drawdown_pct(nav_series: Sequence[float]) -> float:
    """最大回撤（正数百分比）。"""
    peak = None
    max_dd = 0.0
    for v in nav_series:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            dd = (peak - v) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 4)


def should_halt(
    nav_series: Sequence[float],
    daily_pnl_pct: float,
    consecutive_negative_funding: int,
    cfg: RiskConfig,
) -> tuple[bool, list[str]]:
    """是否需要停止交易（平仓并停手）。返回 (是否停止, 原因列表)。"""
    reasons: list[str] = []
    dd = drawdown_pct(nav_series)
    if dd >= cfg.max_drawdown_pct:
        reasons.append(f"组合回撤 {dd:.2f}% >= {cfg.max_drawdown_pct:.0f}%")
    if daily_pnl_pct <= -cfg.max_daily_loss_pct:
        reasons.append(f"单日亏损 {daily_pnl_pct:.2f}% <= -{cfg.max_daily_loss_pct:.0f}%")
    if consecutive_negative_funding >= cfg.funding_negative_halt:
        reasons.append(f"连续 {consecutive_negative_funding} 次负资金费 >= {cfg.funding_negative_halt}")
    return (len(reasons) > 0, reasons)


def round_qty(qty: float, sz_decimals: int) -> float:
    """按交易所 szDecimals 向下取整数量，避免超买。"""
    if sz_decimals < 0:
        raise ValueError("sz_decimals 必须 >= 0")
    factor = 10 ** sz_decimals
    return math.floor(qty * factor) / factor


def order_plan(
    symbol: str,
    notional_usd: float,
    spot_mid: float,
    perp_mid: float,
    spot_sz_decimals: int,
    perp_sz_decimals: int,
    max_lev: int,
) -> dict:
    """生成 delta-neutral 入场订单（现货买 + 永续空），数量按精度向下取整。"""
    spot_qty = round_qty(notional_usd / spot_mid, spot_sz_decimals)
    perp_qty = round_qty(notional_usd / perp_mid, perp_sz_decimals)
    return {
        "symbol": symbol,
        "spot": {"side": "BUY", "qty": spot_qty, "ref_price": spot_mid,
                 "notional_usd": round(spot_qty * spot_mid, 2)},
        "perp": {"side": "SELL", "qty": perp_qty, "ref_price": perp_mid,
                 "notional_usd": round(perp_qty * perp_mid, 2)},
        "perp_margin_usd": round(notional_usd / max_lev, 2),
        "note": "delta-neutral 建仓：现货买入 + 永续等名义做空",
    }
