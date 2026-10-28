#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日历史回测：拉取最新 PURR/HYPE 资金费与日线，重新跑 carry 策略回测。

说明：
- 这是**历史回测**（用已发生的行情），不是实盘、不是纸面前向交易；
- 口径与冻结的 R174A/历史验证一致：$1,000、PURR/HYPE 各 $500 sleeve、1x delta-neutral、
  tier-0 taker + 15bps/腿（0.415%/边）、资金费按日汇总；
- 每次运行追加一行到 daily_backtest_history.csv，便于看指标随时间的变化。

输出：DAILY_BACKTEST.md / daily_backtest.json / daily_backtest_history.csv
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "data_daily_backtest"
OUT_JSON = HERE / "daily_backtest.json"
OUT_MD = HERE / "DAILY_BACKTEST.md"
HIST = HERE / "daily_backtest_history.csv"
BASE_URL = "https://api.hyperliquid.xyz/info"
CAPITAL = 1000.0
SLEEVE = CAPITAL / 2
COST_RATE = 0.00045 + 0.00070 + 2 * 0.0015
ASSETS = {
    "PURR": {"pair": "PURR/USDC", "max_lev": 3},
    "HYPE": {"pair": "@107", "max_lev": 10},
}

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)


def post(body: dict) -> object:
    last = None
    for i in range(4):
        try:
            req = urllib.request.Request(BASE_URL, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"API 失败: {last}")


def cached(name: str, producer) -> object:
    CACHE.mkdir(exist_ok=True)
    safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in name)
    path = CACHE / f"{safe}.json"
    if path.exists() and path.stat().st_size > 1000 \
            and (time.time() - path.stat().st_mtime) < 6 * 3600:
        return json.loads(path.read_text(encoding="utf-8"))
    data = producer()
    path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    return data


def fetch_funding(coin: str) -> list[dict]:
    def _do():
        rows: list[dict] = []
        start = 0
        for _ in range(300):
            batch = post({"type": "fundingHistory", "coin": coin, "startTime": start})
            if not batch:
                break
            rows.extend(batch)
            last = int(batch[-1]["time"])
            if last <= start:
                break
            start = last + 1
            if len(batch) < 500:
                break
        return rows
    return cached(f"funding_{coin}", _do)


def fetch_candles(coin: str) -> list[dict]:
    return cached(f"candles_{coin}", lambda: post({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1d", "startTime": 0},
    }))


def build_panel() -> pd.DataFrame:
    merged = None
    for name, cfg in ASSETS.items():
        funding = fetch_funding(name)
        spot = fetch_candles(cfg["pair"])
        perp = fetch_candles(name)
        spot_close = {datetime.fromtimestamp(int(b["t"]) / 1000, tz=timezone.utc).date(): float(b["c"])
                      for b in spot}
        perp_close = {datetime.fromtimestamp(int(b["t"]) / 1000, tz=timezone.utc).date(): float(b["c"])
                      for b in perp}
        fund_day: dict = {}
        for r in funding:
            d = datetime.fromtimestamp(int(r["time"]) / 1000, tz=timezone.utc).date()
            fund_day[d] = fund_day.get(d, 0.0) + float(r["fundingRate"])
        dates = sorted(set(spot_close) & set(perp_close) & set(fund_day))
        rows = []
        for d in dates:
            s, p = spot_close[d], perp_close[d]
            if not s or not p or not (0.5 <= s / p <= 2.0):
                continue
            rows.append({"day": d, "fund": fund_day[d], "spot": s, "perp": p})
        df = pd.DataFrame(rows)
        df[f"{name}_fund"] = df["fund"]
        df[f"{name}_sret"] = df["spot"].pct_change()
        df[f"{name}_pret"] = df["perp"].pct_change()
        merged = df[["day", f"{name}_fund", f"{name}_sret", f"{name}_pret"]] if merged is None \
            else merged.merge(df[["day", f"{name}_fund", f"{name}_sret", f"{name}_pret"]], on="day", how="inner")
    merged = merged.dropna(subset=[f"{n}_{k}" for n in ASSETS for k in ("fund", "sret", "pret")])
    return merged.sort_values("day").reset_index(drop=True)


def simulate(merged: pd.DataFrame) -> dict:
    n = len(merged)
    if n < 30:
        return {"status": "INSUFFICIENT_DATA", "days": n}
    days = (merged["day"].iloc[-1] - merged["day"].iloc[0]).days
    pnl = np.zeros(n)
    funding_usd = basis_usd = 0.0
    anomalies = 0
    for name, cfg in ASSETS.items():
        N = SLEEVE / (1 + 1 / cfg["max_lev"])
        funding_usd += float(merged[f"{name}_fund"].sum()) * N
        sr = merged[f"{name}_sret"].to_numpy()
        pr = merged[f"{name}_pret"].to_numpy()
        ok = np.abs(sr - pr) <= 0.10
        anomalies += int((~ok).sum())
        basis = np.where(ok, (sr - pr) * N, 0.0)
        basis_usd += float(basis.sum())
        pnl += merged[f"{name}_fund"].to_numpy() * N + basis
    pnl[0] -= COST_RATE * sum(SLEEVE / (1 + 1 / c["max_lev"]) for c in ASSETS.values())
    total = float(pnl.sum()) / CAPITAL
    cagr = (1 + total) ** (365 / max(days, 1)) - 1
    daily_ret = pnl / CAPITAL
    vol = float(daily_ret.std(ddof=1) * np.sqrt(365)) if n > 2 else 0.0
    sharpe = float(daily_ret.mean() / daily_ret.std(ddof=1) * np.sqrt(365)) if n > 2 and daily_ret.std(ddof=1) > 0 else 0.0
    eq = CAPITAL + np.cumsum(pnl)
    peak = np.maximum.accumulate(eq)
    mdd = float(((peak - eq) / peak).max())
    # 近 30 天
    last30 = pnl[-30:]
    ret30 = float(last30.sum()) / CAPITAL
    cagr30 = (1 + ret30) ** (365 / 30) - 1
    return {
        "status": "OK",
        "period": f"{merged['day'].iloc[0]} → {merged['day'].iloc[-1]}",
        "days": int(days),
        "n_days": n,
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "vol_pct": round(vol * 100, 2),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(mdd * 100, 2),
        "funding_usd": round(funding_usd, 2),
        "basis_usd": round(basis_usd, 2),
        "last30_return_pct": round(ret30 * 100, 2),
        "last30_cagr_pct": round(cagr30 * 100, 2),
        "anomaly_days": anomalies,
    }


def main() -> int:
    merged = build_panel()
    result = simulate(merged)
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 追加每日历史
    today = datetime.now().strftime("%Y-%m-%d")
    hist_rows = []
    if HIST.exists():
        hist_rows = pd.read_csv(HIST).to_dict("records")
    if not any(str(r.get("date")) == today for r in hist_rows):
        import csv as _csv
        exists = HIST.exists()
        with HIST.open("a", newline="", encoding="utf-8") as f:
            w = _csv.writer(f)
            if not exists:
                w.writerow(["date", "data_end", "cagr_pct", "vol_pct", "sharpe",
                            "max_dd_pct", "last30_cagr_pct", "funding_usd", "basis_usd"])
            if result.get("status") == "OK":
                w.writerow([today, result["period"].split("→")[-1].strip(), result["cagr_pct"],
                            result["vol_pct"], result["sharpe"], result["max_dd_pct"],
                            result["last30_cagr_pct"], result["funding_usd"], result["basis_usd"]])

    md = [
        "# 每日历史回测（PURR + HYPE carry）", "",
        f"- 运行时间：{result['generated_at']}",
        f"- 回测区间：{result.get('period', '—')}（{result.get('days', 0)} 天 / {result.get('n_days', 0)} 个交易日）",
        "- 口径：$1,000、PURR/HYPE 各 $500、1x delta-neutral、0.415%/边成本；**历史回测，不是实盘**",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 全期总收益 | {result.get('total_return_pct', 0):+.2f}% |",
        f"| 全期年化（CAGR） | {result.get('cagr_pct', 0):+.2f}% |",
        f"| 年化波动 | {result.get('vol_pct', 0):.2f}% |",
        f"| Sharpe | {result.get('sharpe', 0):.2f} |",
        f"| 最大回撤 | {result.get('max_dd_pct', 0):.2f}% |",
        f"| 资金费贡献 | ${result.get('funding_usd', 0):+,.2f} |",
        f"| basis 贡献 | ${result.get('basis_usd', 0):+,.2f} |",
        f"| **最近 30 天收益** | **{result.get('last30_return_pct', 0):+.2f}%**（年化 {result.get('last30_cagr_pct', 0):+.2f}%） |",
        "",
        "## 说明",
        "",
        "- 数据来自 Hyperliquid 官方 API（资金费 + 日线），每日自动更新一次；",
        "- 每次运行会把当日结果追加到 `daily_backtest_history.csv`，可看指标随时间变化；",
        "- 回测是「用历史数据检验策略」，不产生真实收益；实盘需要用户授权且与 paper 前向验证分开。",
        "",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0 if result.get("status") == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())


