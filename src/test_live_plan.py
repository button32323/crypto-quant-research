#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘下单计划（live_order_plan.build_plan）单测：验证风控拒绝路径与数量取整。

运行：python test_live_plan.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from live_order_plan import build_plan  # noqa: E402

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


def market() -> dict:
    return {
        "PURR": {"perp": "PURR", "spot_pair": "PURR/USDC", "spot_mid": 0.0945,
                 "perp_mid": 0.0948, "funding_rate": 0.0000125,
                 "spot_day_vol_usd": 600000.0, "perp_day_vol_usd": 5_000_000.0,
                 "spot_sz_decimals": 0, "perp_sz_decimals": 0, "max_lev": 3},
        "HYPE": {"perp": "HYPE", "spot_pair": "@107", "spot_mid": 76.10,
                 "perp_mid": 76.05, "funding_rate": -0.000005,
                 "spot_day_vol_usd": 1e8, "perp_day_vol_usd": 1e8,
                 "spot_sz_decimals": 2, "perp_sz_decimals": 2, "max_lev": 10},
    }


def main() -> int:
    # 正常
    r = build_plan(market(), 1000.0)
    check("正常 → READY", r["status"] == "READY_FOR_AUTHORIZATION", r["status"])
    check("无拒绝原因", r["rejects"] == [], str(r["rejects"]))
    check("计划数=资产数", len(r["plans"]) == 2, str(len(r["plans"])))
    purr = next(p for p in r["plans"] if p["symbol"] == "PURR")
    hype = next(p for p in r["plans"] if p["symbol"] == "HYPE")
    check("PURR 现货整数取整", float(purr["spot"]["qty"]).is_integer())
    check("PURR 保证金 = 375/3", abs(purr["perp_margin_usd"] - 125.0) < 0.01)
    check("HYPE 数量两位小数", abs(hype["spot"]["qty"] * 100 - round(hype["spot"]["qty"] * 100)) < 1e-6)
    check("HYPE 保证金 = 454.55/10", abs(hype["perp_margin_usd"] - 45.455) < 0.01)
    check("风险表含阈值 12", r["risk_profile"]["funding_negative_halt"] == 12)

    # basis 过大 → NO_GO
    m = market()
    m["PURR"]["perp_mid"] = m["PURR"]["spot_mid"] * 1.01  # +100bps
    r2 = build_plan(m, 1000.0)
    check("basis 超限 → NO_GO", r2["status"] == "NO_GO")
    check("捕获 basis 原因", any("basis" in x for x in r2["rejects"]), str(r2["rejects"]))

    # 资金费异常 → NO_GO
    m2 = market()
    m2["PURR"]["funding_rate"] = 0.02  # 200bps/h
    r3 = build_plan(m2, 1000.0)
    check("funding 异常 → NO_GO", r3["status"] == "NO_GO")
    check("捕获 funding 原因", any("funding" in x for x in r3["rejects"]), str(r3["rejects"]))

    # 容量不足 → NO_GO
    m3 = market()
    m3["PURR"]["spot_day_vol_usd"] = 1000.0  # 1% = $10 < notional $375
    r4 = build_plan(m3, 1000.0)
    check("容量不足 → NO_GO", r4["status"] == "NO_GO")
    check("捕获容量原因", any("成交额" in x for x in r4["rejects"]), str(r4["rejects"]))

    # 资本过小（低于最小下单）→ NO_GO
    r5 = build_plan(market(), 20.0)
    check("资本过小 → NO_GO", r5["status"] == "NO_GO")
    check("捕获最小下单原因", any("最小下单" in x for x in r5["rejects"]), str(r5["rejects"]))

    # 渲染 Markdown 包含关键内容
    from live_order_plan import render_md
    md = render_md(r)
    check("MD 含状态", "READY_FOR_AUTHORIZATION" in md)
    check("MD 含执行顺序", "执行顺序" in md)
    check("MD 含授权清单", "授权要求" in md)

    print(f"\nPASS={PASS} FAIL={FAIL} TOTAL={PASS+FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
