#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容量分析：当前策略（PURR+HYPE）最多能承载多少资本？收入天花板多少？

口径：单资产名义 ≤ 日现货成交额 × capacity_frac（默认 1%）；等权 sleeve；1x delta-neutral。
输出：CAPACITY.md / capacity.json
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import paper_hl_carry as ph  # noqa: E402
from risk_rules import RiskConfig  # noqa: E402

OUT_JSON = HERE / "capacity.json"
OUT_MD = HERE / "CAPACITY.md"
ANNUALIZED_SCENARIOS = {"p5": 14.29, "p50": 19.17, "p95": 24.93}  # bootstrap 分位（历史验证）


def main() -> int:
    cfg = RiskConfig(capital_usd=1000.0)
    ctx = ph.fetch_contexts()
    n = len(ctx)
    rows = []
    caps = []
    mins = []
    for name, m in ctx.items():
        # sleeve = capital/n ；notional = sleeve / (1 + 1/max_lev) = capital * (max_lev/(max_lev+1))/n
        factor = (1.0 / n) * m["max_lev"] / (m["max_lev"] + 1)
        cap_spot = cfg.capacity_frac * m["spot_day_ntl"] / factor
        cap_perp = cfg.capacity_frac * m["perp_day_ntl"] / factor
        min_cap = cfg.min_order_usd / factor
        caps.append(cap_spot)
        mins.append(min_cap)
        rows.append({
            "asset": name,
            "notional_factor": round(factor, 6),
            "spot_day_ntl_usd": round(m["spot_day_ntl"]),
            "perp_day_ntl_usd": round(m["perp_day_ntl"]),
            "max_capital_by_spot_usd": round(cap_spot),
            "max_capital_by_perp_usd": round(cap_perp),
            "min_capital_min_order_usd": round(min_cap, 2),
        })
    cap_total = min(caps)
    min_total = max(mins)
    income = {k: round(cap_total * v / 100, 2) for k, v in ANNUALIZED_SCENARIOS.items()}
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capacity_frac": cfg.capacity_frac,
        "max_capital_usd": round(cap_total),
        "min_capital_usd": round(min_total, 2),
        "max_annual_income_usd": income,
        "assets": rows,
        "note": "容量为动态值（按当前 24h 成交额）；实盘前需重新计算。",
    }
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# 策略容量分析（当前策略能承载多少资本）", "",
        f"- 生成时间：{result['generated_at']}",
        f"- 口径：单资产名义 ≤ 日现货成交额 × {cfg.capacity_frac:.0%}；PURR+HYPE 等权；1x delta-neutral",
        "",
        "| 资产 | 24h 现货成交额 | 24h 永续成交额 | 按容量可承载资本 | 最小资本(最小下单) |",
        "|---|---:|---:|---:|---:|",
    ]
    for r in rows:
        md.append(f"| {r['asset']} | ${r['spot_day_ntl_usd']:,} | ${r['perp_day_ntl_usd']:,} | "
                  f"${r['max_capital_by_spot_usd']:,} | ${r['min_capital_min_order_usd']:,.0f} |")
    md += [
        "",
        "## 结论",
        "",
        f"- **当前策略可承载资本上限 ≈ ${cap_total:,.0f}**（受最低容量资产约束，动态）；",
        f"- 最小可行资本 ≈ ${min_total:,.0f}（低于此，单资产名义不足以满足最小下单）；",
        f"- 按历史 bootstrap 年化：上限资本对应年收入 p5 **${income['p5']:,.0f}** / p50 **${income['p50']:,.0f}** / p95 **${income['p95']:,.0f}**；",
        "- 超过容量上限时，加资本不会等比例增加收入（只能减少单资产权重或增加新资产，而后者需要重新验证）。",
        "",
        "## 说明",
        "",
        "- 容量按**日成交额 1%** 的保守假设；实际可用容量还受盘口深度、滑点、他人竞争影响；",
        "- 当前可执行资产只有 PURR/HYPE（STABLE/AZTEC 受容量限制不可执行，见 R174A 报告）；",
        "- 该数字随行情变化，实盘部署前必须用 `capacity_analysis.py` 重新计算。",
        "",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
