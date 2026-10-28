#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收入预期：把历史验证/压力测试/paper 状态折算成"不同资本能赚多少"。

用法：
    python income_expectation.py                # 生成 INCOME_EXPECTATION.md/json
    python income_expectation.py --capital 5000 # 额外打印 5000 美元资本的收入区间
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HIST = HERE / "historical_validation.json"
CAPACITY = HERE / "capacity.json"
PAPER_VALID = HERE / "validation.json"
READINESS = HERE / "readiness.json"
OUT_JSON = HERE / "income_expectation.json"
OUT_MD = HERE / "INCOME_EXPECTATION.md"
FX = 7.2  # USD -> CNY（估算用）

DEFAULT_CAPITALS = [1000, 5000, 10000, 50000, 100000]


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def cny(usd: float) -> str:
    v = usd * FX
    if abs(v) >= 1e4:
        return f"¥{v/1e4:,.2f}万"
    return f"¥{v:,.0f}"


def pct(x: float) -> str:
    return f"{x:+.1f}%"


def main() -> int:
    hist = load(HIST)
    capinfo = load(CAPACITY)
    max_capital = float(capinfo.get("max_capital_usd") or 0) or None
    paper = load(PAPER_VALID)
    ready = load(READINESS)
    boot = hist.get("bootstrap", {})
    stress = hist.get("stress_tests", [])
    base_cagr = hist.get("daily_full", {}).get("cagr_pct", 0.0)

    if boot.get("status") != "OK":
        print("缺少 historical_validation.json 的 bootstrap 结果；请先运行 historical_validation.py")
        return 1

    p5 = boot["annualized_p5"] / 100
    p50 = boot["annualized_p50"] / 100
    p95 = boot["annualized_p95"] / 100
    stress_map = {s["scenario"]: s for s in stress}

    def income(capital: float, rate: float) -> dict:
        annual = capital * rate
        return {"annual_usd": annual, "monthly_usd": annual / 12}

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"historical_base_cagr_pct": base_cagr,
                   "max_deployable_capital_usd": max_capital,
                   "bootstrap_p5_p50_p95": [boot["annualized_p5"], boot["annualized_p50"], boot["annualized_p95"]],
                   "paper_status": paper.get("status"), "readiness": ready.get("overall")},
        "fx_usd_cny": FX,
        "capital_table": [],
        "target_capital": [],
        "stress_income": [],
    }

    for cap in DEFAULT_CAPITALS:
        deployable = min(cap, max_capital) if max_capital else cap
        row = {"capital_usd": cap, "deployable_usd": round(deployable, 2),
               "capped": bool(max_capital and cap > max_capital)}
        for label, rate in (("p5", p5), ("p50", p50), ("p95", p95)):
            inc = income(deployable, rate)
            row[label] = {"annual_usd": round(inc["annual_usd"], 2),
                          "monthly_usd": round(inc["monthly_usd"], 2),
                          "annual_cny": round(inc["annual_usd"] * FX, 0),
                          "monthly_cny": round(inc["monthly_usd"] * FX, 0)}
        result["capital_table"].append(row)

    for monthly_cny in (1000, 3000, 5000):
        annual_cny = monthly_cny * 12
        row = {"monthly_cny": monthly_cny, "annual_cny": annual_cny}

        for label, rate in (("p5", p5), ("p50", p50), ("p95", p95)):
            cap_cny = annual_cny / rate
            cap_usd = cap_cny / FX
            row[label] = {"capital_cny": round(cap_cny, 0),
                          "capital_usd": round(cap_usd, 0),
                          "feasible": (max_capital is None or cap_usd <= max_capital)}
        result["target_capital"].append(row)

    for scen in ("资金费减半", "资金费归零", "最差 90 天资金费环境（年化外推，不计 basis）"):
        if scen in stress_map:
            rate = stress_map[scen]["cagr_pct"] / 100
            result["stress_income"].append({
                "scenario": scen, "annualized_pct": stress_map[scen]["cagr_pct"],
                "at_10k_annual_usd": round(10000 * rate, 0),
                "at_10k_monthly_cny": round(10000 * rate / 12 * FX, 0),
                "at_50k_annual_usd": round(50000 * rate, 0),
                "at_50k_monthly_cny": round(50000 * rate / 12 * FX, 0),
            })

    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# 收入预期（基于历史验证与压力测试）",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 数据来源：历史验证（日线 685 天）+ Bootstrap（2000 次）+ 压力测试；paper 状态：**{paper.get('status', 'NO_DATA')}**；"
        f"就绪状态：**{ready.get('overall', 'UNKNOWN')}**",
        "- 汇率假设：1 USD = ¥7.2（仅用于展示，实际按你换汇价）",
        "",
        "## 一句话",
        "",
        f"这是一个历史年化约 **{pct(base_cagr)}** 的低风险 carry 策略；"
        f"收入 ≈ **资本 × 年化**。$1,000 资本 → 年收入约 **$150–250**；"
        "要成为有意义的收入，需要 **$10,000 以上**的资本。",
        "",
        "## 资本 → 收入（Bootstrap 分位）",
        "",
        "> 注意：当前策略有**容量上限**（见 `CAPACITY.md`，约 $14k，受 PURR 现货成交额约束）；超过上限的资本无法部署，下表收入按「可部署规模」计算。",
        "",
        "| 资本(USD) | 可部署规模 | p5 年化 | p5 年收入 | p50 年化 | p50 年收入 | p95 年化 | p95 年收入 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["capital_table"]:
        md.append(
            f"| ${row['capital_usd']:,} | ${row['deployable_usd']:,.0f}"
            + ("（容量封顶）" if row.get("capped") else "") + " | "
            + f"{pct(boot['annualized_p5'])} | "
            f"${row['p5']['annual_usd']:,.0f}（{cny(row['p5']['annual_usd'])}/年） | "
            f"{pct(boot['annualized_p50'])} | ${row['p50']['annual_usd']:,.0f}（{cny(row['p50']['annual_usd'])}/年） | "
            f"{pct(boot['annualized_p95'])} | ${row['p95']['annual_usd']:,.0f}（{cny(row['p95']['annual_usd'])}/年） |"
        )
    md += [
        "",
        "> 月收入 = 年收入 ÷ 12；上表为扣成本后的历史/统计口径，不代表未来。",
        "",
        "## 目标月收入 → 所需资本（按 p5 / p50 / p95）",
        "",
        "| 目标月收入 | p5 所需资本 | p50 所需资本 | p95 所需资本 |",
        "|---:|---:|---:|---:|",
    ]
    for row in result["target_capital"]:
        def mark(label):
            return "" if row[label].get("feasible", True) else "（超过容量）"
        md.append(
            f"| ¥{row['monthly_cny']:,} | ¥{row['p5']['capital_cny']/1e4:,.2f}万（${row['p5']['capital_usd']:,.0f}）{mark('p5')} | "
            f"¥{row['p50']['capital_cny']/1e4:,.2f}万（${row['p50']['capital_usd']:,.0f}）{mark('p50')} | "
            f"¥{row['p95']['capital_cny']/1e4:,.2f}万（${row['p95']['capital_usd']:,.0f}）{mark('p95')} |"
        )
    md += [
        "",
        f"> **容量约束**：当前策略可承载资本上限约 ${max_capital:,.0f}，对应月收入上限约 **${max_capital * p50 / 12:,.0f}（p50）**；"
        "表中标注「超过容量」的目标在当前策略下无法达到，需要先验证新资产（重新历史验证 + paper），不能靠加杠杆。",
        "",
        "## 压力情景下的收入",
        "",
        "| 情景 | 年化 | $10k 年收入 | $10k 月收入 | $50k 年收入 | $50k 月收入 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for s in result["stress_income"]:
        md.append(f"| {s['scenario']} | {pct(s['annualized_pct'])} | ${s['at_10k_annual_usd']:,.0f} | "
                  f"¥{s['at_10k_monthly_cny']:,.0f} | ${s['at_50k_annual_usd']:,.0f} | "
                  f"¥{s['at_50k_monthly_cny']:,.0f} |")
    md += [
        "",
        "## 风险提示（必须一起读）",
        "",
        f"- 历史最大回撤：日线 {hist.get('daily_full', {}).get('max_dd_pct', '—')}%、小时级 {hist.get('hourly', {}).get('max_dd_pct', '—')}%；",
        "- 收益来源高度依赖资金费：资金费减半年化约 +8.7%，归零则约 −1.4%；",
        "- Bootstrap 只反映历史样本内抽样不确定性，不包含资金费制度变化、交易所/合约/预言机风险；",
        "- 实际收入随资本、执行滑点、资金费环境变化，本文件不是收益承诺。",
        "",
        "## 下一步",
        "",
        "- 30 天 paper 验证进行中（预注册通过条件见 `VALIDATION.md`）；",
        "- 需要你提供：真实本金、可承受最大亏损、交易所账户与逐笔授权；",
        "- 若资本 < $1,000，建议先通过其他方式积累本金，或把该策略当学习/现金管理工具。",
        "",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")

    print(json.dumps(result["source"], ensure_ascii=False, indent=2))
    print(f"\n已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




