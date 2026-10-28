#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""今年（YTD）收益回测：PURR+HYPE carry 策略在指定年份的表现。

用法：python ytd_backtest.py [年份，默认今年]
输出：YTD_<year>_BACKTEST.md / ytd_<year>_backtest.json
注意：历史回测，不产生真实收益、不下单。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import daily_backtest as db  # noqa: E402

CAPITAL = db.CAPITAL
SLEEVE = db.SLEEVE
COST_RATE = db.COST_RATE
ASSETS = db.ASSETS


def sim_asset(df: pd.DataFrame, name: str) -> dict:
    cfg = ASSETS[name]
    N = SLEEVE / (1 + 1 / cfg["max_lev"])
    days = max((df["day"].iloc[-1] - df["day"].iloc[0]).days, 1)
    pnl = df[f"{name}_fund"].to_numpy() * N
    sr = df[f"{name}_sret"].to_numpy()
    pr = df[f"{name}_pret"].to_numpy()
    ok = np.abs(sr - pr) <= 0.10
    pnl = pnl + np.where(ok, (sr - pr) * N, 0.0)
    pnl[0] -= COST_RATE * N
    total = float(pnl.sum()) / SLEEVE
    eq = SLEEVE + np.cumsum(pnl)
    peak = np.maximum.accumulate(eq)
    mdd = float(((peak - eq) / peak).max()) if len(eq) else 0.0
    return {
        "asset": name,
        "period": f"{df['day'].iloc[0]} → {df['day'].iloc[-1]}",
        "days": int(days),
        "return_pct": round(total * 100, 2),
        "annualized_pct": round(((1 + total) ** (365 / days) - 1) * 100, 2),
        "max_dd_pct": round(mdd * 100, 2),
        "funding_usd": round(float(df[f"{name}_fund"].sum()) * N, 2),
        "basis_usd": round(float(np.where(ok, sr - pr, 0.0).sum()) * N, 2),
    }


def monthly(df: pd.DataFrame) -> list[dict]:
    n = len(df)
    pnl = np.zeros(n)
    for name, cfg in ASSETS.items():
        N = SLEEVE / (1 + 1 / cfg["max_lev"])
        sr = df[f"{name}_sret"].to_numpy()
        pr = df[f"{name}_pret"].to_numpy()
        ok = np.abs(sr - pr) <= 0.10
        pnl += df[f"{name}_fund"].to_numpy() * N + np.where(ok, (sr - pr) * N, 0.0)
    pnl[0] -= COST_RATE * sum(SLEEVE / (1 + 1 / c["max_lev"]) for c in ASSETS.values())
    tmp = df[["day"]].copy()
    tmp["pnl"] = pnl
    tmp["month"] = pd.to_datetime(tmp["day"]).dt.strftime("%Y-%m")
    g = tmp.groupby("month")["pnl"].sum()
    return [{"month": m, "return_pct": round(float(v) / CAPITAL * 100, 2)} for m, v in g.items()]


def main() -> int:
    year = int(sys.argv[1]) if len(sys.argv) > 1 else datetime.now().year
    merged = db.build_panel()
    df = merged[pd.to_datetime(merged["day"]).dt.year == year].reset_index(drop=True)
    if len(df) < 10:
        print(f"{year} 年数据不足（{len(df)} 天）")
        return 1
    result = db.simulate(df)
    result["year"] = year
    result["per_asset"] = [sim_asset(df, name) for name in ASSETS]
    result["monthly"] = monthly(df)
    result["capital"] = CAPITAL
    out_json = HERE / f"ytd_{year}_backtest.json"
    out_md = HERE / f"YTD_{year}_BACKTEST.md"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        f"# {year} 年收益回测（PURR + HYPE carry）", "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 区间：{result['period']}（{result['days']} 天 / {result['n_days']} 个交易日）",
        f"- 口径：${CAPITAL:,.0f}、PURR/HYPE 各 $500、1x delta-neutral、0.415%/边；**历史回测，非实盘**",
        "",
        "## 组合（$1,000 资本）",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 年内总收益 | **{result['total_return_pct']:+.2f}%**（≈ ${result['total_return_pct']/100*CAPITAL:+,.2f}） |",
        f"| 折算年化 | {result['cagr_pct']:+.2f}% |",
        f"| 年化波动 | {result['vol_pct']:.2f}% |",
        f"| Sharpe | {result['sharpe']:.2f} |",
        f"| 最大回撤 | {result['max_dd_pct']:.2f}% |",
        f"| 资金费贡献 | ${result['funding_usd']:+,.2f} |",
        f"| basis 贡献 | ${result['basis_usd']:+,.2f} |",
        "",
        "## 分资产（各 $500 sleeve）",
        "",
        "| 资产 | 收益 | 年化 | 最大回撤 | 资金费 | basis |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for a in result["per_asset"]:
        md.append(f"| {a['asset']} | {a['return_pct']:+.2f}% | {a['annualized_pct']:+.2f}% | "
                  f"{a['max_dd_pct']:.2f}% | ${a['funding_usd']:+,.2f} | ${a['basis_usd']:+,.2f} |")
    md += ["", "## 月度收益（组合）", "", "| 月份 | 收益 |", "|---|---:|"]
    for m in result["monthly"]:
        md.append(f"| {m['month']} | {m['return_pct']:+.2f}% |")
    md += [
        "",
        "## 说明",
        "",
        "- 收益按 $1,000 计算；资本放大时收益近似线性（容量上限约 $12–14k）；",
        "- 最近月份收益偏低与资金费环境弱（历史 30 天窗口约 7% 分位）一致；",
        "- 历史回测不代表未来，也不产生真实收益。",
        "",
    ]
    out_md.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n已生成: {out_json}\n已生成: {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
