#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAV 独立对账逻辑单测（不联网）。

运行：python test_reconcile_nav.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from reconcile_nav import compute_nav  # noqa: E402

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


def base_state() -> dict:
    return {
        "capital_usd": 1000.0,
        "entry_cost_usd": 3.44,
        "funding_accrued_usd": 0.08,
        "positions": {
            "PURR": {"qty_spot": 3966.0, "qty_perp": 3955.0,
                     "entry_spot": 0.0945, "entry_perp": 0.0948},
            "HYPE": {"qty_spot": 5.97, "qty_perp": 5.97,
                     "entry_spot": 76.10, "entry_perp": 76.05},
        },
    }


def main() -> int:
    ctx_flat = {"PURR": {"spot_mid": 0.0945, "perp_mid": 0.0948},
                "HYPE": {"spot_mid": 76.10, "perp_mid": 76.05}}
    nav = compute_nav(base_state(), ctx_flat)
    check("价格不变时 NAV=capital-cost+funding", abs(nav - (1000 - 3.44 + 0.08)) < 1e-6, str(nav))

    # 现货涨 1%、永续涨 1%：两腿近似抵消（delta-neutral），NAV 基本不变
    ctx_up = {"PURR": {"spot_mid": 0.0945 * 1.01, "perp_mid": 0.0948 * 1.01},
              "HYPE": {"spot_mid": 76.10 * 1.01, "perp_mid": 76.05 * 1.01}}
    nav_up = compute_nav(base_state(), ctx_up)
    check("同涨 1% 时净敞口影响很小", abs(nav_up - nav) < 5.0, f"{nav_up} vs {nav}")

    # 仅现货涨、永续不动：应体现 basis 变化
    ctx_spot = {"PURR": {"spot_mid": 0.0945 * 1.02, "perp_mid": 0.0948},
                "HYPE": {"spot_mid": 76.10, "perp_mid": 76.05}}
    nav_spot = compute_nav(base_state(), ctx_spot)
    expected = nav + 3966.0 * (0.0945 * 0.02)
    check("仅现货上涨时 NAV 按 qty 变化", abs(nav_spot - expected) < 1e-6, f"{nav_spot} vs {expected}")

    # 仅永续跌：空头盈利
    ctx_perp = {"PURR": {"spot_mid": 0.0945, "perp_mid": 0.0948 * 0.98},
                "HYPE": {"spot_mid": 76.10, "perp_mid": 76.05}}
    nav_perp = compute_nav(base_state(), ctx_perp)
    expected_perp = nav + 3955.0 * (0.0948 * 0.02)
    check("仅永续下跌时空头盈利", abs(nav_perp - expected_perp) < 1e-6, f"{nav_perp} vs {expected_perp}")

    print(f"\nPASS={PASS} FAIL={FAIL} TOTAL={PASS+FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
