#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金费对账逻辑单测（不联网，mock API）。

运行：python test_reconcile_funding.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import reconcile_funding as rf  # noqa: E402

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
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capital_usd": 1000.0,
        "funding_accrued_usd": 0.075 - 0.045455,
        "last_funding_ts_ms": {"PURR": 10_800_001, "HYPE": 10_800_002},
        "funding_stats": {"PURR": {"settlements": 2, "positive": 2},
                          "HYPE": {"settlements": 2, "positive": 0}},
        "positions": {
            "PURR": {"perp": "PURR", "notional_usd": 375.0},
            "HYPE": {"perp": "HYPE", "notional_usd": 454.55},
        },
    }


def fake_fetch(coin: str, start: int) -> list[dict]:
    if coin == "PURR":
        return [{"fundingRate": "0.0001", "time": 7_200_001},
                {"fundingRate": "0.0001", "time": 10_800_001}]
    return [{"fundingRate": "-0.00005", "time": 7_200_002},
            {"fundingRate": "-0.00005", "time": 10_800_002}]


def main() -> int:
    # 正常对账
    r = rf.reconcile(base_state(), fake_fetch)
    check("正常对账 PASS", r["overall"] == "PASS", str(r["checks"]))
    check("累计金额一致", abs(r["diff_usd"]) < 1e-6, str(r["diff_usd"]))
    check("结算次数检查通过", all(c["ok"] for c in r["checks"]))

    # 结算次数不一致
    s = base_state()
    s["funding_stats"]["PURR"]["settlements"] = 1
    r2 = rf.reconcile(s, fake_fetch)
    check("结算次数不一致 → FAIL", r2["overall"] == "FAIL")
    check("捕获结算次数差异", any("结算次数" in c["check"] and not c["ok"] for c in r2["checks"]))

    # 金额不一致
    s3 = base_state()
    s3["funding_accrued_usd"] = 0.5
    r3 = rf.reconcile(s3, fake_fetch)
    check("金额不一致 → FAIL", r3["overall"] == "FAIL")
    check("捕获金额差异", any("累计资金费" in c["check"] and not c["ok"] for c in r3["checks"]))

    # 时间戳未消费到底
    s4 = base_state()
    s4["last_funding_ts_ms"]["HYPE"] = 1
    r4 = rf.reconcile(s4, fake_fetch)
    check("时间戳未消费 → FAIL", r4["overall"] == "FAIL")
    check("捕获时间戳问题", any("时间戳" in c["check"] and not c["ok"] for c in r4["checks"]))

    print(f"\nPASS={PASS} FAIL={FAIL} TOTAL={PASS+FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
