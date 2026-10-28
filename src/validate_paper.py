#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评估 paper trading 是否达到 30 天验证通过条件。

用法：
    python validate_paper.py
输出：
    validation.json / VALIDATION.md（同时打印摘要）

门禁在 2026-10-24 启动时预注册，不因结果修改：
  - 运行 ≥ 30 天
  - 实现收益率 > 0
  - 年化收益率 > 10%
  - 最大回撤 < 5%
  - 资金费正费率占比 > 60%
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
LOG = HERE / "paper_log.csv"
OUT_JSON = HERE / "validation.json"
OUT_MD = HERE / "VALIDATION.md"

MIN_DAYS = 30
MIN_ANNUALIZED = 10.0
MAX_DD = 5.0
MIN_FUNDING_SHARE = 0.60


def load_rows() -> list[dict]:
    if not LOG.exists():
        return []
    with LOG.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def evaluate(created: datetime, rows: list[dict], state: dict,
             now: datetime | None = None,
             funding_reconcile: dict | None = None) -> dict:
    """纯函数：给定创建时间、paper 日志行与 state，返回门禁评估结果。

    funding_reconcile：reconcile_funding.py 的输出（预注册条件「数据对账干净」）。
    未满 30 天时不参与判定；满 30 天时必须是 PASS。
    """
    now = now or datetime.now(timezone.utc)
    days = (now - created).total_seconds() / 86400

    latest = rows[-1] if rows else {}
    nav = float(latest.get("nav_usd") or state.get("initial_nav_usd") or state["capital_usd"])
    capital = float(state["capital_usd"])
    ret_pct = (nav / capital - 1) * 100

    max_dd = 0.0
    peak = None
    for r in rows:
        v = float(r.get("nav_usd") or 0)
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            dd = (peak - v) / peak * 100
            max_dd = max(max_dd, dd)

    stats = state.get("funding_stats", {})
    settlements = sum(v.get("settlements", 0) for v in stats.values())
    positives = sum(v.get("positive", 0) for v in stats.values())
    funding_share = positives / settlements if settlements else 0.0

    annualized = ret_pct * 365 / days if days >= 1 else None  # 不足 1 天不年化，避免夸大

    checks = {
        "elapsed_30d": days >= MIN_DAYS,
        "positive_return": ret_pct > 0,
        "annualized_gt_10": (annualized is not None and annualized > MIN_ANNUALIZED),
        "max_dd_lt_5": max_dd < MAX_DD,
        "funding_positive_share_gt_60": funding_share > MIN_FUNDING_SHARE,
        # 数据对账：只有满 30 天正式判定时才要求 PASS（PENDING 阶段不阻塞）
        "funding_reconcile_pass": (days < MIN_DAYS
                                   or (funding_reconcile or {}).get("overall") == "PASS"),
    }
    if days < MIN_DAYS:
        status = "PENDING"
    else:
        status = "PASS" if all(checks.values()) else "FAIL"

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "days_elapsed": round(days, 4),
        "capital_usd": capital,
        "nav_usd": round(nav, 4),
        "return_pct": round(ret_pct, 4),
        "annualized_pct": round(annualized, 2) if annualized is not None else None,
        "max_drawdown_pct": round(max_dd, 4),
        "funding_settlements": settlements,
        "funding_positive": positives,
        "funding_positive_share": round(funding_share, 4),
        "funding_reconcile_overall": (funding_reconcile or {}).get("overall"),
        "checks": checks,
        "status": status,
        "gate": {
            "min_days": MIN_DAYS,
            "min_annualized_pct": MIN_ANNUALIZED,
            "max_drawdown_pct": MAX_DD,
            "min_funding_positive_share": MIN_FUNDING_SHARE,
            "funding_reconcile_required": True,
        },
    }


def render_md(result: dict) -> str:
    status = result["status"]
    ret_pct = result["return_pct"]
    checks = result["checks"]

    def mark(b: bool) -> str:
        return "✅" if b else "❌"

    md = [
        "# Paper Trading 30 天验证评估",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 已运行：**{result['days_elapsed']:.2f} 天 / {MIN_DAYS} 天**",
        f"- 状态：**{status}**",
        "",
        "| 指标 | 当前值 | 通过条件 | 判定 |",
        "|---|---:|---|---|",
        f"| 实现收益率 | {ret_pct:+.4f}% | > 0 | {mark(checks['positive_return'])} |",
        f"| 年化收益率 | {result['annualized_pct'] if result['annualized_pct'] is not None else '—'} "
        f"| > {MIN_ANNUALIZED:.0f}% | {mark(checks['annualized_gt_10'])} |",
        f"| 最大回撤 | {result['max_drawdown_pct']:.4f}% | < {MAX_DD:.0f}% | {mark(checks['max_dd_lt_5'])} |",
        f"| 资金费正费率占比 | {result['funding_positive_share']:.1%}"
        f"（{result['funding_positive']}/{result['funding_settlements']}） | > {MIN_FUNDING_SHARE:.0%} "
        f"| {mark(checks['funding_positive_share_gt_60'])} |",
        f"| 运行天数 | {result['days_elapsed']:.2f} | ≥ {MIN_DAYS} | {mark(checks['elapsed_30d'])} |",
        f"| 资金费独立对账 | {result['funding_reconcile_overall'] or '—'} | PASS（满 30 天时） "
        f"| {mark(checks['funding_reconcile_pass'])} |",
        "",
    ]
    if status == "PENDING":
        md += ["> 尚未满 30 天：只做观察，不做结论；不做实盘。", ""]
    elif status == "PASS":
        md += ["> 通过：可以进入实盘讨论阶段。实盘仍需用户逐笔授权，且先小资金、严格风控。", ""]
    else:
        md += ["> 未通过：不做实盘。记录原因，必要时回到研究阶段（不得调参凑数）。", ""]
    md += [
        "## 说明",
        "",
        "- 本评估完全基于虚拟资金 paper 数据，不涉及任何真实下单。",
        "- 通过条件在 2026-10-24 启动时预注册，不因结果修改。",
        "",
    ]
    return "\n".join(md)


def main() -> int:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    rows = load_rows()
    created = datetime.fromisoformat(state["created_at"])
    reconcile_path = HERE / "funding_reconcile.json"
    funding_reconcile = (json.loads(reconcile_path.read_text(encoding="utf-8"))
                         if reconcile_path.exists() else None)
    result = evaluate(created, rows, state, funding_reconcile=funding_reconcile)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_md(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
