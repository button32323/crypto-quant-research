#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金费环境分析：当前费率在历史上处于什么分位？滚动 30 天年化分布如何？

用途：判断"现在是不是跑 carry 的好时候"，以及 paper 30 天可能面对的资金费环境。
输出：FUNDING_ENVIRONMENT.md / funding_environment.json
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
R174A = Path(r"Path(__import__("os").environ.get("R174A_DIR", "data/R174A_HL_SPOT_PERP_CARRY"))\data")
OUT_JSON = HERE / "funding_environment.json"
DIST_JSON = HERE / "funding_env_distribution.json"
OUT_MD = HERE / "FUNDING_ENVIRONMENT.md"
CAPITAL = 1000.0
SLEEVE = CAPITAL / 2
NOTIONAL = {"PURR": SLEEVE / (1 + 1 / 3), "HYPE": SLEEVE / (1 + 1 / 10)}  # 375 / 454.55


def load_rates(name: str) -> dict[int, float]:
    rows = json.loads((R174A / "funding" / f"{name}.json").read_text(encoding="utf-8"))
    out = {}
    for r in rows:
        h = int(r["time"]) // 3_600_000
        out[h] = float(r["fundingRate"])
    return out


def main() -> int:
    rates = {name: load_rates(name) for name in NOTIONAL}
    hours = sorted(set(rates["PURR"]) & set(rates["HYPE"]))
    if len(hours) < 100:
        print("数据不足")
        return 1
    # 组合每小时资金费（USD）与年化收益率（按 $1,000 资本）
    usd = np.array([rates["PURR"][h] * NOTIONAL["PURR"] + rates["HYPE"][h] * NOTIONAL["HYPE"]
                    for h in hours])
    ann_hourly = usd * 24 * 365 / CAPITAL * 100  # 单小时年化

    def rolling_annualized(window_hours: int) -> np.ndarray:
        if len(ann_hourly) < window_hours:
            return np.array([])
        csum = np.cumsum(np.insert(usd, 0, 0.0))
        mean_usd = (csum[window_hours:] - csum[:-window_hours]) / window_hours
        return mean_usd * 24 * 365 / CAPITAL * 100

    result = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "n_hours": len(hours), "windows": {}}
    for label, wh in (("24h", 24), ("7d", 24 * 7), ("30d", 24 * 30)):
        arr = rolling_annualized(wh)
        if len(arr) == 0:
            continue
        result["windows"][label] = {
            "n": int(len(arr)),
            "p5": round(float(np.percentile(arr, 5)), 2),
            "p25": round(float(np.percentile(arr, 25)), 2),
            "p50": round(float(np.percentile(arr, 50)), 2),
            "p75": round(float(np.percentile(arr, 75)), 2),
            "p95": round(float(np.percentile(arr, 95)), 2),
            "mean": round(float(arr.mean()), 2),
            "share_below_10pct": round(float((arr < 10).mean()), 4),
            "share_negative": round(float((arr < 0).mean()), 4),
        }

    # 当前值（从 paper state 读取）
    state_path = HERE / "state.json"
    current = None
    if state_path.exists():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        current = round(float(st.get("expected_funding_per_hour") or 0) * 24 * 365
                        / float(st["capital_usd"]) * 100, 2)
    # 当前值在历史 30 天窗口中的分位
    if current is not None:
        arr30 = rolling_annualized(24 * 30)
        result["current_annualized_pct"] = current
        result["current_percentile_30d"] = (
            round(float((arr30 < current).mean()) * 100, 1) if len(arr30) else None)
        result["current_vs_median_30d"] = (
            round(current - float(np.median(arr30)), 2) if len(arr30) else None)

    # 当前环境下的 30 天预期（资金费口径）
    if current is not None:
        expected_funding_30d = current * 30 / 365
        entry_cost_pct = (375.0 + 454.55) / CAPITAL * 0.00415 * 100  # 建仓成本占资本 %
        result["expected_30d_funding_pct"] = round(expected_funding_30d, 2)
        result["entry_cost_pct_of_capital"] = round(entry_cost_pct, 2)
        result["expected_30d_net_pct"] = round(expected_funding_30d - entry_cost_pct, 2)

    # 每日追加环境历史（便于看 30 天环境变化趋势）
    try:
        import csv as _csv
        hist_path = HERE / "funding_env_history.csv"
        today = datetime.now().strftime("%Y-%m-%d")
        existing = []
        if hist_path.exists():
            with hist_path.open(encoding="utf-8", newline="") as f:
                existing = list(_csv.DictReader(f))
        if not any(r.get("date") == today for r in existing):
            with hist_path.open("a", newline="", encoding="utf-8") as f:
                w = _csv.writer(f)
                if not existing:
                    w.writerow(["date", "current_annualized_pct", "percentile_30d", "vs_median_30d"])
                w.writerow([today, current, result.get("current_percentile_30d"),
                            result.get("current_vs_median_30d")])
    except Exception:  # noqa: BLE001
        pass

    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 保存 30 天窗口的分位数网格（供 paper REPORT 实时计算环境分位）
    arr30 = rolling_annualized(24 * 30)
    if len(arr30):
        quantiles = [round(float(np.percentile(arr30, q)), 3) for q in range(101)]
        DIST_JSON.write_text(json.dumps({
            "generated_at": result["generated_at"],
            "window": "30d",
            "n": int(len(arr30)),
            "quantiles_p0_to_p100": quantiles,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    md = [
        "# 资金费环境分析",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 数据：R174A 逐小时资金费，{len(hours)} 个共同小时（约 {len(hours)/24/30:.1f} 个月）",
        "- 口径：$1,000 资本、PURR/HYPE 双 sleeve、按当前费率外推年化（不含成本/basis）",
        "",
    ]
    if current is not None:
        md += [
            f"## 当前处于历史什么位置",
            "",
            f"- 当前费率隐含年化：**{current:+.2f}%**",
            f"- 历史 30 天滚动窗口分位：**{result['current_percentile_30d']:.0f}% 分位**",
            f"- 相对历史 30 天中位：{result['current_vs_median_30d']:+.2f}pp",
            "",
            "## 当前环境下的 30 天预期（资金费口径，不含 basis）",
            "",
            f"- 以当前年化 {current:+.1f}% 外推 30 天：资金费约 **{result.get('expected_30d_funding_pct', 0):+.2f}%**；",
            f"- 建仓成本约 {result.get('entry_cost_pct_of_capital', 0):.2f}%（占资本）；",
            f"- 净预期约 **{result.get('expected_30d_net_pct', 0):+.2f}%**（未计 basis 波动）；",
            "- 门禁要求 30 天年化 >10% ≈ 月收益 >0.79%：当前环境下**大概率不通过**——"
            "这是环境问题，不是策略实现错误；paper 会如实记录。",
            "",
        ]
    md += [
        "## 滚动窗口年化分布（资金费口径）",
        "",
        "| 窗口 | p5 | p25 | 中位 | p75 | p95 | 均值 | <10% 时间占比 | 负值时间占比 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, w in result["windows"].items():
        md.append(f"| {label} | {w['p5']:+.1f}% | {w['p25']:+.1f}% | {w['p50']:+.1f}% | "
                  f"{w['p75']:+.1f}% | {w['p95']:+.1f}% | {w['mean']:+.1f}% | "
                  f"{w['share_below_10pct']:.1%} | {w['share_negative']:.1%} |")
    md += [
        "",
        "## 解读",
        "",
        "- 这张表回答「现在资金费环境好不好」：当前值分位越低，越不适合期待高收益；",
        "- `<10% 时间占比` 是历史 30 天窗口中年化不足 10% 的比例——paper 想要通过门禁（>10%），"
        "需要恰好落在资金费相对好的窗口；",
        "- 注意：这是**资金费**口径，不含 basis 与成本；实际 paper 收益还会被 basis 波动与建仓成本影响。",
        "",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



