#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每周复盘：paper 每满 7 天生成一份 WEEK_N.md（不重复生成）。

由更新器每日任务调用；day<7 时跳过。
输出：WEEK_1.md、WEEK_2.md ...（30 天验证共 4 份）
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
VALIDATION = HERE / "validation.json"
LOG = HERE / "paper_log.csv"
ENV = HERE / "funding_environment.json"


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def latest_nav() -> float | None:
    if not LOG.exists():
        return None
    with LOG.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return float(rows[-1]["nav_usd"]) if rows else None


def main() -> int:
    state = load(STATE)
    val = load(VALIDATION)
    env = load(ENV)
    days = float(val.get("days_elapsed") or 0)
    week = int(days // 7)
    if week < 1:
        print(f"day={days:.2f}，未满 7 天，跳过")
        return 0
    out = HERE / f"WEEK_{week}.md"
    if out.exists():
        print(f"WEEK_{week}.md 已存在，跳过")
        return 0

    nav = latest_nav() or state.get("capital_usd", 1000)
    capital = float(state.get("capital_usd", 1000))
    ret = (nav / capital - 1) * 100
    stats = state.get("funding_stats", {})
    settlements = sum(v.get("settlements", 0) for v in stats.values())
    positives = sum(v.get("positive", 0) for v in stats.values())
    share = positives / settlements if settlements else 0.0
    env_ann = env.get("current_annualized_pct")
    env_pct = env.get("current_percentile_30d")
    annualized = val.get("annualized_pct")
    status = val.get("status", "—")

    # 最近结算明细（来自逐笔审计日志）
    settlements_log = state.get("settlements_log", {})
    entries = []
    for asset, items in settlements_log.items():
        for e in items:
            entries.append((int(e["time"]), asset, float(e["rate"]), float(e["usd"])))
    entries.sort()
    recent = entries[-10:]

    lines = [
        f"# Paper Trading 第 {week} 周复盘（Day {days:.1f}/30）",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 验证状态：**{status}**",
        "",
        "| 指标 | 当前 | 门禁/参考 |",
        "|---|---:|---|",
        f"| 净值 | ${nav:,.2f} | 初始 ${capital:,.0f} |",
        f"| 累计收益率 | {ret:+.4f}% | > 0 |",
        f"| 年化（线性折算） | {annualized if annualized is not None else '—'} | > 10% |",
        f"| 资金费累计 | ${float(state.get('funding_accrued_usd') or 0):+.4f} | — |",
        f"| 结算次数 / 正费率占比 | {settlements} / {share:.1%} | 正占比 > 60% |",
        f"| 最大回撤 | {val.get('max_drawdown_pct', '—')}% | < 5% |",
        f"| 当前资金费环境分位 | {env_pct if env_pct is not None else '—'}% | 越高越有利 |",
        f"| 当前费率隐含年化 | {env_ann if env_ann is not None else '—'}% | > 10% 才好 |",
        "",
        "## 最近结算明细（最多 10 笔）",
        "",
        "| 时间(UTC) | 资产 | 费率 | 资金费($) |",
        "|---|---|---:|---:|",
    ]
    from datetime import timezone as _tz
    for ts, asset, rate, usd in recent:
        t = datetime.fromtimestamp(ts / 1000, tz=_tz.utc).strftime("%m-%d %H:%M")
        lines.append(f"| {t} | {asset} | {rate*1e4:+.4f} bps | ${usd:+.6f} |")
    lines += [
        "",
        "## 本周观察要点",
        "",
        "1. 资金费是否每周结算稳定（次数 ≈ 7×24×2 资产）；",
        "2. 正费率占比是否回到 60% 以上；",
        "3. 环境分位是否回升（历史 30 天窗口）；",
        "4. 是否触发任何风控告警（`watchdog.log` 中的 ALERT）。",
        "",
        "> 本复盘自动生成；不改变预注册门禁。到期决策见 `PAPER_FAILURE_PLAYBOOK.md`。",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成 {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

