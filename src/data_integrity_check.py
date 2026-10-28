#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史数据完整性检查（验证我们全部结论所依赖的数据本身是否自洽）。

检查项：
1. 日线面板的每日资金费 = 逐小时 fundingHistory 的当日之和；
2. 日线面板的 spot/perp 收盘价 = 1d K 线收盘价；
3. 1h K 线的当日最后收盘价 = 1d K 线收盘价（重叠区间）。

输出：DATA_INTEGRITY.md / data_integrity.json；退出码 0=PASS，1=FAIL。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
R174A = Path(r"Path(__import__("os").environ.get("R174A_DIR", "data/R174A_HL_SPOT_PERP_CARRY"))\data")
OUT_JSON = HERE / "data_integrity.json"
OUT_MD = HERE / "DATA_INTEGRITY.md"
ASSETS = ("PURR", "HYPE")
FUNDING_TOL = 1e-10
PRICE_REL_TOL = 1e-9


def load_daily_candles(path: Path) -> dict:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {datetime.fromtimestamp(int(r["t"]) / 1000, tz=timezone.utc).date(): float(r["c"])
            for r in rows}


def load_hourly_candles(path: Path) -> dict:
    rows = json.loads(path.read_text(encoding="utf-8"))
    out: dict = {}
    for r in rows:
        dt = datetime.fromtimestamp(int(r["t"]) / 1000, tz=timezone.utc)
        out[dt.date()] = {"close": float(r["c"]), "hour": dt.hour}  # 记录当日最后一根 1h 的小时
    return out


def main() -> int:
    checks: list[dict] = []
    details: dict = {}

    for name in ASSETS:
        panel = pd.read_csv(R174A / "panels" / f"daily_{name}.csv")
        panel["day"] = pd.to_datetime(panel["day"]).dt.date
        panel = panel[panel["tradable"] == 1].dropna(subset=["fund_day"]).set_index("day")

        # 1) 资金费
        fund = json.loads((R174A / "funding" / f"{name}.json").read_text(encoding="utf-8"))
        df = pd.DataFrame(fund)
        df["dt"] = pd.to_datetime(df["time"], unit="ms", utc=True)
        df["date"] = df["dt"].dt.date
        df["rate"] = df["fundingRate"].astype(float)
        daily_sum = df.groupby("date")["rate"].sum()
        joined = panel.join(daily_sum.rename("hourly_sum"))
        missing = int(joined["hourly_sum"].isna().sum())
        diff = (joined["fund_day"] - joined["hourly_sum"]).abs()
        max_diff = float(diff.max()) if len(diff) else 0.0
        ok = missing == 0 and max_diff <= FUNDING_TOL
        checks.append({"check": f"{name} 资金费日汇总一致", "ok": ok,
                       "detail": f"{len(joined)} 天，缺小时数据 {missing} 天，最大差 {max_diff:.3e}"})
        details[f"{name}_funding_max_diff"] = max_diff

        # 2) 日线收盘价
        for kind, col in (("spot", "spot_close"), ("perp", "perp_close")):
            candles = load_daily_candles(R174A / "candles" / f"1d_{kind}_{name}.json")
            pairs = [(d, float(row[col]), candles[d]) for d, row in panel.iterrows() if d in candles]
            rel = max((abs(p - c) / c for _, p, c in pairs if c), default=0.0)
            ok = len(pairs) > 0 and rel <= PRICE_REL_TOL
            checks.append({"check": f"{name} {kind} 日线收盘与 1d K 线一致", "ok": ok,
                           "detail": f"对比 {len(pairs)} 天，最大相对差 {rel:.3e}"})
            details[f"{name}_{kind}_rel_diff"] = rel

        # 3) 1h -> 日线收盘（只比较 1h 数据覆盖完整的已收盘日；数据抓取截断的前一天会天然不一致）
        hourly = load_hourly_candles(R174A / "candles" / f"1h_spot_{name}.json")
        daily = load_daily_candles(R174A / "candles" / f"1d_spot_{name}.json")
        today = datetime.now(timezone.utc).date()
        overlap = [d for d in hourly
                   if d in daily and d < today and hourly[d]["hour"] >= 23]
        rel = max((abs(hourly[d]["close"] - daily[d]) / daily[d] for d in overlap if daily[d]), default=0.0)
        ok = len(overlap) > 0 and rel <= PRICE_REL_TOL
        checks.append({"check": f"{name} 1h 末收盘与 1d 收盘一致", "ok": ok,
                       "detail": f"重叠 {len(overlap)} 个 1h 覆盖完整的已收盘日，最大相对差 {rel:.3e}"})
        details[f"{name}_hourly_rel_diff"] = rel

    overall = "PASS" if all(c["ok"] for c in checks) else "FAIL"
    result = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "overall": overall, "tolerances": {"funding": FUNDING_TOL, "price_rel": PRICE_REL_TOL},
              "checks": checks, "details": details}
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# 历史数据完整性检查",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 数据：R174A 面板 / 资金费 / 1d、1h K 线；整体：**{overall}**",
        "",
        "| 检查 | 结果 | 详情 |",
        "|---|---|---|",
    ]
    for c in checks:
        md.append(f"| {c['check']} | {'✅' if c['ok'] else '❌'} | {c['detail']} |")
    md += [
        "",
        "## 说明",
        "",
        "- 资金费：日线面板的 `fund_day` 应等于逐小时 fundingHistory 当日之和；",
        "- 价格：面板收盘价应等于 1d K 线收盘价；1h K 线的当日最后收盘也应等于 1d 收盘；",
        "- 容差：资金费 1e-10、价格相对误差 1e-9（数据同源时应几乎为 0）。",
        "",
        "> 历史验证的全部结论依赖这些数据；本检查确认输入数据自洽，避免把数据错误误读为策略表现。",
        "",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("overall", "details")}, ensure_ascii=False, indent=2))
    for c in checks:
        print(f"{'PASS' if c['ok'] else 'FAIL'} {c['check']}")
    print(f"已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
