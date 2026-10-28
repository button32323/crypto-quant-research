#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""30 天到期最终复盘：自动生成 FINAL_REVIEW.md（day<30 时跳过）。

由更新器每日任务调用；满 30 天自动生成，作为"是否进入实盘"的决策包。
输出：FINAL_REVIEW.md
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
VALIDATION = HERE / "validation.json"
ENV = HERE / "funding_environment.json"
FUND_REC = HERE / "funding_reconcile.json"
NAV_REC = HERE / "nav_reconcile.json"
HEALTH = HERE / "health.json"
OUT = HERE / "FINAL_REVIEW.md"
MIN_DAYS = 30


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    state = load(STATE)
    val = load(VALIDATION)
    env = load(ENV)
    fr = load(FUND_REC)
    nr = load(NAV_REC)
    hl = load(HEALTH)
    days = float(val.get("days_elapsed") or 0)
    if days < MIN_DAYS:
        print(f"day={days:.2f} < {MIN_DAYS}，跳过")
        return 0

    status = val.get("status", "—")
    checks = val.get("checks", {})
    capital = float(state.get("capital_usd", 1000))
    nav = float(val.get("nav_usd") or capital)
    ret = float(val.get("return_pct") or 0)
    stats = state.get("funding_stats", {})
    settlements = sum(v.get("settlements", 0) for v in stats.values())
    positives = sum(v.get("positive", 0) for v in stats.values())

    verdict = "✅ 通过，可进入实盘讨论（仍需用户逐笔授权）" if status == "PASS" else "❌ 未通过，不做实盘"
    lines = [
        "# Paper Trading 30 天最终复盘",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 运行：{days:.2f} 天 ｜ 虚拟本金 ${capital:,.0f} ｜ 最终净值 ${nav:,.2f}",
        f"- **判定：{verdict}**",
        "",
        "## 预注册门禁",
        "",
        "| 条件 | 结果 | 判定 |",
        "|---|---|---|",
        f"| 运行 ≥ 30 天 | {days:.2f} 天 | {'✅' if checks.get('elapsed_30d') else '❌'} |",
        f"| 实现收益率 > 0 | {ret:+.4f}% | {'✅' if checks.get('positive_return') else '❌'} |",
        f"| 年化 > 10% | {val.get('annualized_pct')}% | {'✅' if checks.get('annualized_gt_10') else '❌'} |",
        f"| 最大回撤 < 5% | {val.get('max_drawdown_pct')}% | {'✅' if checks.get('max_dd_lt_5') else '❌'} |",
        f"| 正费率占比 > 60% | {val.get('funding_positive_share')}（{val.get('funding_positive')}/{val.get('funding_settlements')}） | {'✅' if checks.get('funding_positive_share_gt_60') else '❌'} |",
        f"| 资金费独立对账 PASS | {val.get('funding_reconcile_overall')} | {'✅' if checks.get('funding_reconcile_pass') else '❌'} |",
        "",
        "## 30 天关键数字",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 累计收益 | {ret:+.4f}%（${nav - capital:+,.2f}） |",
        f"| 资金费累计 | ${float(state.get('funding_accrued_usd') or 0):+.4f} |",
        f"| 结算次数 / 正费率占比 | {settlements} 次 / {val.get('funding_positive_share')} |",
        f"| 最大回撤 | {val.get('max_drawdown_pct')}% |",
        f"| 建仓成本 | ${float(state.get('entry_cost_usd') or 0):.2f} |",
        "",
        "## 对账与数据质量",
        "",
        f"- 资金费独立对账：**{fr.get('overall', '—')}**（差异 ${fr.get('diff_usd', '—')}）",
        f"- NAV 独立对账：**{nr.get('overall', '—')}**（差异 ${nr.get('diff', '—')}）",
        f"- 系统体检：**{hl.get('overall', '—')}**",
        "",
        "## 资金费环境（结束时）",
        "",
        f"- 当前费率隐含年化：{env.get('current_annualized_pct', '—')}%",
        f"- 历史 30 天窗口分位：{env.get('current_percentile_30d', '—')}%",
        "",
        "## 若 PASS：实盘部署建议（仍需用户逐笔授权）",
        "",
        "- 部署规模：**≤ 85% 容量上限**（当前容量约 $13–14k，建议 ≤$12k），下单前重跑 `capacity_analysis.py` 与 `live_order_plan.py`；",
        "- 首笔规模 ≤ 可承受亏损的 10%；",
        "- 执行：按 `LIVE_RUNBOOK.md`（先买现货 → 立即开永续空 → 对账必须 PASS）；",
        "- 风控：总杠杆 ≤1x、连续 12 次负资金费平仓、单日亏 2% 停止、回撤 5% 全平；",
        "- 账户：API key 仅交易权限、禁提现。",
        "",
        "## 下一步",
        "",
        "1. 若 PASS：按 `LIVE_RUNBOOK.md` 进入实盘讨论；需要用户提供真实本金/账户/逐笔授权，首笔 ≤ 可承受亏损 10%；",
        "2. 若 FAIL：按 `PAPER_FAILURE_PLAYBOOK.md` 处理——**不做实盘**；可选停止，或按完全相同规则重跑新 30 天；",
        "3. 无论结果：把本文件 + `VALIDATION.md` + `历史数据验证报告.pdf` 作为完整证据链存档。",
        "",
        "> 本复盘自动生成，不改变任何预注册条件；实盘仍需用户逐笔授权。",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成 {OUT.name}（status={status}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

