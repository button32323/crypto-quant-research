#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paper trading 每日日志（JOURNAL.md）：每天追加一行，形成 30 天时间线。

由更新器每日任务调用；重复运行同一天不会重复追加。
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "paper_log.csv"
STATE = HERE / "state.json"
VALIDATION = HERE / "validation.json"
JOURNAL = HERE / "JOURNAL.md"

HEADER = """# Paper Trading 日志（每日一行）

> 自动生成：记录每日净值、资金费、验证进度；30 天验证结束后作为复盘依据。

| 日期 | 天数 | 净值 | 收益率 | 资金费累计 | 结算次数 | 正占比 | 状态 |
|---|---:|---:|---:|---:|---:|---:|---|
"""


def latest_log_row() -> dict:
    if not LOG.exists():
        return {}
    with LOG.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-1] if rows else {}


def main() -> int:
    if not STATE.exists():
        print("state.json 不存在")
        return 0
    state = json.loads(STATE.read_text(encoding="utf-8"))
    val = json.loads(VALIDATION.read_text(encoding="utf-8")) if VALIDATION.exists() else {}
    row = latest_log_row()
    today = datetime.now().strftime("%Y-%m-%d")

    if JOURNAL.exists():
        text = JOURNAL.read_text(encoding="utf-8")
        if f"| {today} |" in text:
            print(f"{today} 已记录，跳过")
            return 0
    else:
        text = HEADER

    nav = float(row.get("nav_usd") or state.get("initial_nav_usd") or state["capital_usd"])
    capital = float(state["capital_usd"])
    ret = (nav / capital - 1) * 100
    stats = state.get("funding_stats", {})
    settlements = sum(v.get("settlements", 0) for v in stats.values())
    positives = sum(v.get("positive", 0) for v in stats.values())
    share = positives / settlements if settlements else 0.0
    line = (
        f"| {today} | {float(val.get('days_elapsed') or 0):.2f}/30 | ${nav:,.2f} | {ret:+.3f}% | "
        f"${float(state.get('funding_accrued_usd') or 0):+.4f} | {settlements} | {share:.1%} | {val.get('status', '—')} |\n"
    )
    JOURNAL.write_text(text + line, encoding="utf-8")
    print(f"已追加 {today} 到 {JOURNAL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
