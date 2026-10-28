#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""30 天验证门禁（validate_paper.evaluate）单测：确保 PASS/FAIL 判定正确。

运行：python test_validate_paper.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from validate_paper import evaluate  # noqa: E402

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


def make_state(settlements: int = 10, positive: int = 8, capital: float = 1000.0) -> dict:
    return {
        "created_at": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds"),
        "capital_usd": capital,
        "initial_nav_usd": capital - 3.44,
        "funding_stats": {"PURR": {"settlements": settlements // 2, "positive": positive // 2},
                          "HYPE": {"settlements": settlements - settlements // 2,
                                   "positive": positive - positive // 2}},
    }


def rows(navs: list[float]) -> list[dict]:
    return [{"nav_usd": str(v), "ts": "2026-10-24T00:00:00+00:00"} for v in navs]


def main() -> int:
    now = datetime.now(timezone.utc)
    created = now - timedelta(days=31)

    # 1) 正常 PASS：31 天，nav 1030（+3%，年化约 35%），回撤小，资金费占比 80%，对账 PASS
    r = evaluate(created, rows([1000, 1005, 1020, 1030]), make_state(), now,
                 funding_reconcile={"overall": "PASS"})
    check("PASS 场景状态", r["status"] == "PASS", r["status"])
    check("PASS 五个条件全过", all(r["checks"].values()), str(r["checks"]))
    check("年化 >10", r["annualized_pct"] > 10, str(r["annualized_pct"]))

    # 2) PENDING：不足 30 天，即便指标很好也是 PENDING
    r2 = evaluate(now - timedelta(days=10), rows([1000, 1030]), make_state(), now)
    check("不足30天 → PENDING", r2["status"] == "PENDING", r2["status"])
    check("不足1天不年化", evaluate(now - timedelta(hours=5), rows([1000]),
                                make_state(), now)["annualized_pct"] is None)

    # 3) FAIL：收益为负
    r3 = evaluate(created, rows([1000, 990]), make_state(), now)
    check("负收益 → FAIL", r3["status"] == "FAIL", r3["status"])
    check("negative_return 标记", r3["checks"]["positive_return"] is False)

    # 4) FAIL：年化不足 10%（31 天 +0.5%）
    r4 = evaluate(created, rows([1000, 1005]), make_state(), now)
    check("年化不足 → FAIL", r4["status"] == "FAIL", r4["status"])
    check("annualized_gt_10 标记", r4["checks"]["annualized_gt_10"] is False, str(r4["annualized_pct"]))

    # 5) FAIL：最大回撤 6%（>5% 门）
    r5 = evaluate(created, rows([1000, 1100, 1034]), make_state(), now)
    check("回撤 6% → FAIL", r5["status"] == "FAIL", r5["status"])
    check("max_dd 标记", r5["checks"]["max_dd_lt_5"] is False, str(r5["max_drawdown_pct"]))

    # 6) FAIL：资金费正占比 50%（<=60% 门）
    r6 = evaluate(created, rows([1000, 1030]), make_state(settlements=10, positive=5), now)
    check("正费率占比 50% → FAIL", r6["status"] == "FAIL", r6["status"])
    check("funding 标记", r6["checks"]["funding_positive_share_gt_60"] is False,
          str(r6["funding_positive_share"]))

    # 7) 边界：恰好 30 天判定为已满（>=）
    r7 = evaluate(now - timedelta(days=30), rows([1000, 1030]), make_state(), now,
                  funding_reconcile={"overall": "PASS"})
    check("恰好30天 → 已满", r7["checks"]["elapsed_30d"] is True)
    check("恰好30天可 PASS", r7["status"] == "PASS", r7["status"])

    # 8) 资金费为 0 时占比 0：PASS 场景下应为 FAIL（数据不足不允许通过）
    r8 = evaluate(created, rows([1000, 1030]), make_state(settlements=0, positive=0), now)
    check("无资金费结算 → FAIL", r8["status"] == "FAIL", r8["status"])

    # 9) 满 30 天但缺少资金费对账 → FAIL（预注册条件：数据对账干净）
    r9 = evaluate(created, rows([1000, 1030]), make_state(), now)
    check("满30天缺对账 → FAIL", r9["status"] == "FAIL", r9["status"])
    check("缺对账标记", r9["checks"]["funding_reconcile_pass"] is False)

    # 10) 对账 FAIL → 整体 FAIL
    r10 = evaluate(created, rows([1000, 1030]), make_state(), now,
                   funding_reconcile={"overall": "FAIL"})
    check("对账 FAIL → 整体 FAIL", r10["status"] == "FAIL", r10["status"])

    # 11) PENDING 阶段对账状态不阻塞
    r11 = evaluate(now - timedelta(days=10), rows([1000, 1030]), make_state(), now)
    check("PENDING 阶段缺对账不阻塞", r11["status"] == "PENDING", r11["status"])

    print(f"\nPASS={PASS} FAIL={FAIL} TOTAL={PASS+FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

