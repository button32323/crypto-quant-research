#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""风控模块单元测试（不联网、不涉及资金）。

运行：python test_risk_rules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from risk_rules import (  # noqa: E402
    RiskConfig,
    check_entry,
    check_portfolio,
    drawdown_pct,
    order_plan,
    round_qty,
    should_halt,
    size_position,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}  {detail}")


def main() -> int:
    cfg = RiskConfig(capital_usd=1000)

    # --- 配置校验 ---
    try:
        RiskConfig(capital_usd=0)
        check("capital=0 应报错", False)
    except ValueError:
        check("capital=0 应报错", True)
    try:
        RiskConfig(capital_usd=1000, max_total_leverage=5)
        check("杠杆>3 应报错", False)
    except ValueError:
        check("杠杆>3 应报错", True)
    check("默认版本存在", cfg.version == "1.0.0")

    # --- 仓位计算（1x delta-neutral） ---
    s = size_position(1000, 2, 3, cfg)
    check("PURR sleeve=500", s.sleeve_usd == 500.0, str(s))
    check("PURR notional=375", abs(s.notional_usd - 375.0) < 1e-6, str(s))
    check("PURR margin=125", abs(s.perp_margin_usd - 125.0) < 1e-6, str(s))
    s2 = size_position(1000, 2, 10, cfg)
    check("HYPE notional=454.55", abs(s2.notional_usd - 454.55) < 0.01, str(s2))
    s3 = size_position(1000, 1, 3, cfg)
    check("单资产被 max_asset_share 限制为 500", s3.sleeve_usd == 500.0, str(s3))

    # --- 入场检查 ---
    ok = check_entry("PURR", 375, 0.0948, 0.0949, 0.0000125, 500_000, cfg)
    check("正常入场通过", ok == [], str(ok))
    bad_basis = check_entry("PURR", 375, 0.10, 0.101, 0.0000125, 500_000, cfg)
    check("basis 超限被拒", any("basis" in r for r in bad_basis), str(bad_basis))
    bad_funding = check_entry("PURR", 375, 0.0948, 0.0949, 0.02, 500_000, cfg)
    check("funding 异常被拒", any("funding" in r for r in bad_funding), str(bad_funding))
    bad_cap = check_entry("AZTEC", 375, 0.02, 0.02, 0.0001, 191, cfg)
    check("容量不足被拒", any("日现货成交额" in r for r in bad_cap), str(bad_cap))
    bad_min = check_entry("PURR", 5, 0.0948, 0.0949, 0.0000125, 500_000, cfg)
    check("低于最小下单被拒", any("最小下单" in r for r in bad_min), str(bad_min))

    # --- 组合检查 ---
    pos_ok = [{"symbol": "PURR", "notional_usd": 375}, {"symbol": "HYPE", "notional_usd": 454.55}]
    check("组合名义 829.55 通过", check_portfolio(pos_ok, 1000, cfg) == [])
    pos_leverage = [{"symbol": "A", "notional_usd": 400},
                    {"symbol": "B", "notional_usd": 400},
                    {"symbol": "C", "notional_usd": 400}]
    check("组合杠杆超限被拒", any("总名义" in r for r in check_portfolio(pos_leverage, 1000, cfg)))
    pos_share = [{"symbol": "PURR", "notional_usd": 600}]
    check("单资产占比超限被拒", any("单资产" in r for r in check_portfolio(pos_share, 1000, cfg)))

    # --- 回撤 ---
    check("回撤计算 0", drawdown_pct([100, 101, 102]) == 0.0)
    check("回撤计算 4.9%", drawdown_pct([100, 105, 99.857]) > 4.8)
    check("回撤计算 5% 边界", abs(drawdown_pct([100, 105, 99.75]) - 5.0) < 0.01)

    # --- 停止规则 ---
    halt, why = should_halt([100, 100, 100], -1, 0, cfg)
    check("正常不停止", halt is False and why == [], str(why))
    halt, why = should_halt([100, 100, 94], -1, 0, cfg)
    check("回撤 6% 触发停止", halt is True and any("回撤" in w for w in why), str(why))
    halt, why = should_halt([100, 100], -2.5, 0, cfg)
    check("单日亏 2.5% 触发停止", halt is True and any("单日" in w for w in why), str(why))
    cfg3 = RiskConfig(capital_usd=1000, funding_negative_halt=3)
    halt, why = should_halt([100, 100], -1, 0, cfg3)
    check("默认阈值 12 时 3 次不停止", halt is False and why == [], str(why))
    halt, why = should_halt([100, 100], -1, 3, cfg3)
    check("自定义阈值 3 时 3 次触发停止", halt is True and any("负资金费" in w for w in why), str(why))
    halt, why = should_halt([100], 0, 12, cfg)
    check("默认阈值 12 次触发停止", halt is True and any("负资金费" in w for w in why), str(why))

    # --- 数量取整 ---
    check("szDecimals=0 向下取整", round_qty(3956.9, 0) == 3956.0)
    check("szDecimals=2 向下取整", round_qty(5.9663, 2) == 5.96)

    # --- 订单计划 ---
    plan = order_plan("PURR", 375, 0.0948, 0.0949, 0, 0, 3)
    check("现货数量整数", plan["spot"]["qty"] == int(plan["spot"]["qty"]))
    check("永续方向 SELL", plan["perp"]["side"] == "SELL")
    check("保证金 = notional/lev", abs(plan["perp_margin_usd"] - 125.0) < 1e-6, str(plan))

    print(f"\nPASS={PASS} FAIL={FAIL} TOTAL={PASS+FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
