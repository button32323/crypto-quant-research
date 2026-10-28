#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAV 独立对账：用 state 的持仓 + 实时盘口价重算净值，与 paper_log 最新净值对比。

用途：发现"状态被写坏/持仓数量错/成本错"这类问题（资金费对账只覆盖资金费）。
输出：nav_reconcile.json；退出码 0=PASS，1=FAIL。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import paper_hl_carry as ph  # noqa: E402

STATE = HERE / "state.json"
LOG = HERE / "paper_log.csv"
OUT = HERE / "nav_reconcile.json"


def compute_nav(state: dict, ctx: dict) -> float:
    """独立重算 NAV：capital - 建仓成本 + 累计资金费 + 两腿浮动盈亏。"""
    nav = (float(state["capital_usd"]) - float(state["entry_cost_usd"])
           + float(state["funding_accrued_usd"]))
    for name, pos in state["positions"].items():
        m = ctx[name]
        nav += float(pos["qty_spot"]) * (m["spot_mid"] - pos["entry_spot"])
        nav += float(pos["qty_perp"]) * (pos["entry_perp"] - m["perp_mid"])
    return nav


def latest_log_nav() -> float | None:
    if not LOG.exists():
        return None
    import csv
    with LOG.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return float(rows[-1]["nav_usd"]) if rows else None


def main() -> int:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    ctx = ph.fetch_contexts()
    expected = compute_nav(state, ctx)
    logged = latest_log_nav()
    capital = float(state["capital_usd"])
    tolerance = max(0.5, capital * 0.005)  # 盘口与上次快照之间的正常价格波动容许
    diff = (expected - logged) if logged is not None else None
    ok = diff is not None and abs(diff) <= tolerance
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expected_nav": round(expected, 4),
        "logged_nav": round(logged, 4) if logged is not None else None,
        "diff": round(diff, 4) if diff is not None else None,
        "tolerance_usd": round(tolerance, 2),
        "overall": "PASS" if ok else "FAIL",
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
