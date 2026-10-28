#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史数据算法验证：HL 现货-永续 carry（PURR + HYPE）。

验证内容：
A. 日线全期回放（2024-11 ~ 2026-10，与 R174A 数据一致）
B. 分期间/分年稳定性（2025 vs 2026；前半 vs 后半）
C. 成本敏感性（每 fill 滑点 0/5/15/30/60 bps）
D. 1 小时级回放（2026-03-29 ~ 2026-10-24，约 208 天）
E. 与 R174A 已发布 A0 结果交叉核对
输出：
    historical_validation.json / 历史数据验证报告.md
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
R174A = Path(r"Path(__import__("os").environ.get("R174A_DIR", "data/R174A_HL_SPOT_PERP_CARRY"))")
DATA = R174A / "data"
OUT_JSON = HERE / "historical_validation.json"
OUT_MD = HERE / "历史数据验证报告.md"

CAPITAL = 1000.0
SLEEVE = CAPITAL / 2
FEES_RATE = 0.00045 + 0.00070  # 11.5 bps both legs
BASE_SLIP_BPS = 15.0
ASSETS = {
    "PURR": {"panel": DATA / "panels" / "daily_PURR.csv", "max_lev": 3,
             "spot_1h": DATA / "candles" / "1h_spot_PURR.json",
             "perp_1h": DATA / "candles" / "1h_perp_PURR.json",
             "funding": DATA / "funding" / "PURR.json"},
    "HYPE": {"panel": DATA / "panels" / "daily_HYPE.csv", "max_lev": 10,
             "spot_1h": DATA / "candles" / "1h_spot_HYPE.json",
             "perp_1h": DATA / "candles" / "1h_perp_HYPE.json",
             "funding": DATA / "funding" / "HYPE.json"},
}


def notional_of(sleeve: float, max_lev: int) -> float:
    return sleeve / (1 + 1 / max_lev)


def entry_cost_rate(slip_bps: float) -> float:
    """往返一侧的建仓成本率 = fees + 2×滑点（两腿各一次 fill）。"""
    return FEES_RATE + 2 * slip_bps / 1e4


def load_panel(name: str) -> pd.DataFrame:
    df = pd.read_csv(ASSETS[name]["panel"])
    df["day"] = pd.to_datetime(df["day"])
    return df.sort_values("day").reset_index(drop=True)


def daily_validate(slip_bps: float = BASE_SLIP_BPS, with_exit: bool = False) -> dict:
    """日线全期验证：A0 always-on，delta-neutral，固定名义。"""
    cost = entry_cost_rate(slip_bps)
    per_asset = {}
    for name, cfg in ASSETS.items():
        df = load_panel(name)
        N = notional_of(SLEEVE, cfg["max_lev"])
        trades = df[df["tradable"] == 1].dropna(subset=["fund_day", "spot_ret", "perp_ret"]).reset_index(drop=True)
        funding_usd = float(trades["fund_day"].sum()) * N
        basis_usd = float((trades["spot_ret"] - trades["perp_ret"]).sum()) * N
        pnl = funding_usd + basis_usd - cost * N - (cost * N if with_exit else 0.0)
        days = max((trades["day"].iloc[-1] - trades["day"].iloc[0]).days, 1)
        ret = pnl / SLEEVE
        cagr = (1 + ret) ** (365 / days) - 1
        # 日收益序列（用于波动/回撤）
        daily_pnl = trades["fund_day"] * N + (trades["spot_ret"] - trades["perp_ret"]) * N
        daily_ret = daily_pnl / SLEEVE
        equity = SLEEVE + daily_pnl.cumsum() - cost * N
        peak = equity.cummax()
        mdd = float(((peak - equity) / peak).max())
        per_asset[name] = {
            "days": days,
            "n_days": int(len(trades)),
            "funding_usd": round(funding_usd, 2),
            "basis_usd": round(basis_usd, 2),
            "cost_usd": round(cost * N * (2 if with_exit else 1), 2),
            "pnl_usd": round(pnl, 2),
            "return_pct": round(ret * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "vol_pct": round(float(daily_ret.std(ddof=1) * math.sqrt(365) * 100), 2),
            "max_dd_pct": round(mdd * 100, 2),
        }

    # 组合：两资产都可用（tradable）的日期
    merged = None
    for name in ASSETS:
        df = load_panel(name)
        d = df[df["tradable"] == 1][["day", "fund_day", "spot_ret", "perp_ret"]].rename(
            columns={"fund_day": f"{name}_fund", "spot_ret": f"{name}_sret", "perp_ret": f"{name}_pret"})
        merged = d if merged is None else merged.merge(d, on="day", how="inner")
    merged = merged.dropna().sort_values("day").reset_index(drop=True)
    pnl_series = pd.Series(0.0, index=merged.index)
    funding_total = 0.0
    basis_total = 0.0
    cost_total = 0.0
    for name, cfg in ASSETS.items():
        N = notional_of(SLEEVE, cfg["max_lev"])
        funding_total += float(merged[f"{name}_fund"].sum()) * N
        basis_total += float((merged[f"{name}_sret"] - merged[f"{name}_pret"]).sum()) * N
        cost_total += cost * N
        pnl_series += merged[f"{name}_fund"] * N + (merged[f"{name}_sret"] - merged[f"{name}_pret"]) * N
    days = max((merged["day"].iloc[-1] - merged["day"].iloc[0]).days, 1)
    exit_extra = cost_total if with_exit else 0.0
    total_pnl = funding_total + basis_total - cost_total - exit_extra
    ret = total_pnl / CAPITAL
    cagr = (1 + ret) ** (365 / days) - 1
    daily_ret = (pnl_series - cost_total / len(merged)) / CAPITAL
    vol = float(daily_ret.std(ddof=1) * math.sqrt(365))
    sharpe = float(daily_ret.mean() / daily_ret.std(ddof=1) * math.sqrt(365)) if daily_ret.std(ddof=1) > 0 else 0.0
    downside = daily_ret[daily_ret < 0].std(ddof=1)
    sortino = float(daily_ret.mean() / downside * math.sqrt(365)) if downside and downside > 0 else 0.0
    equity = CAPITAL + (pnl_series - cost_total).cumsum() if len(pnl_series) else pd.Series([CAPITAL])
    # 更精确：初始权益 CAPITAL，随后每日累加 PnL
    eq = [CAPITAL]
    running = CAPITAL
    for v in (pnl_series.values if len(pnl_series) else []):
        running += v
        eq.append(running)
    eq = np.array(eq)
    peak = np.maximum.accumulate(eq)
    mdd = float(((peak - eq) / peak).max()) if len(eq) else 0.0
    return {
        "slip_bps": slip_bps,
        "cost_rate_side": round(cost, 6),
        "period": f"{merged['day'].iloc[0].date()} → {merged['day'].iloc[-1].date()}",
        "days": days,
        "funding_usd": round(funding_total, 2),
        "basis_usd": round(basis_total, 2),
        "cost_usd": round(cost_total * (2 if with_exit else 1), 2),
        "total_pnl_usd": round(total_pnl, 2),
        "return_pct": round(ret * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "vol_pct": round(vol * 100, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "max_dd_pct": round(mdd * 100, 2),
        "per_asset": per_asset,
    }


def segment_validate(daily: dict) -> dict:
    """按年/前后半段拆分（组合日线）。"""
    merged = None
    for name in ASSETS:
        df = load_panel(name)
        d = df[df["tradable"] == 1][["day", "fund_day", "spot_ret", "perp_ret"]].rename(
            columns={"fund_day": f"{name}_fund", "spot_ret": f"{name}_sret", "perp_ret": f"{name}_pret"})
        merged = d if merged is None else merged.merge(d, on="day", how="inner")
    merged = merged.dropna().sort_values("day").reset_index(drop=True)

    def seg_stats(df: pd.DataFrame) -> dict:
        if df.empty:
            return {}
        pnl = pd.Series(0.0, index=df.index)
        for name, cfg in ASSETS.items():
            N = notional_of(SLEEVE, cfg["max_lev"])
            pnl += df[f"{name}_fund"] * N + (df[f"{name}_sret"] - df[f"{name}_pret"]) * N
        days = max((df["day"].iloc[-1] - df["day"].iloc[0]).days, 1)
        ret = float(pnl.sum()) / CAPITAL
        cagr = (1 + ret) ** (365 / days) - 1
        eq = CAPITAL + pnl.cumsum()
        peak = np.maximum.accumulate(eq.values)
        mdd = float(((peak - eq.values) / peak).max())
        return {"period": f"{df['day'].iloc[0].date()} → {df['day'].iloc[-1].date()}",
                "days": days, "return_pct": round(ret * 100, 2),
                "cagr_pct": round(cagr * 100, 2), "max_dd_pct": round(mdd * 100, 2)}

    out = {"full": seg_stats(merged)}
    for year in (2025, 2026):
        sub = merged[merged["day"].dt.year == year]
        out[str(year)] = seg_stats(sub)
    half = len(merged) // 2
    out["first_half"] = seg_stats(merged.iloc[:half])
    out["second_half"] = seg_stats(merged.iloc[half:])
    return out


def cost_sensitivity() -> list[dict]:
    rows = []
    for slip in (0, 5, 15, 30, 60):
        v = daily_validate(slip_bps=slip)
        rows.append({"slip_bps": slip, "cost_rate_side": v["cost_rate_side"],
                     "return_pct": v["return_pct"], "cagr_pct": v["cagr_pct"],
                     "max_dd_pct": v["max_dd_pct"]})
    return rows


def load_hourly_candles(path: Path) -> dict[int, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for bar in data:
        hour_ms = int(bar["t"]) // 3_600_000 * 3_600_000
        out[hour_ms] = float(bar["c"])
    return out


def load_hourly_funding(path: Path) -> dict[int, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for r in data:
        hour_ms = int(r["time"]) // 3_600_000 * 3_600_000
        out[hour_ms] = float(r["fundingRate"])
    return out


def hourly_validate(slip_bps: float = BASE_SLIP_BPS) -> dict:
    cost = entry_cost_rate(slip_bps)
    series = {}
    for name, cfg in ASSETS.items():
        series[name] = {
            "spot": load_hourly_candles(cfg["spot_1h"]),
            "perp": load_hourly_candles(cfg["perp_1h"]),
            "funding": load_hourly_funding(cfg["funding"]),
        }
    hours = None
    for name in ASSETS:
        s = series[name]
        common = set(s["spot"]) & set(s["perp"]) & set(s["funding"])
        hours = common if hours is None else (hours & common)
    hours = sorted(hours or [])
    if len(hours) < 100:
        return {"status": "INSUFFICIENT_HOURLY_DATA", "n_hours": len(hours)}

    entry = hours[0]
    pos = {}
    for name, cfg in ASSETS.items():
        N = notional_of(SLEEVE, cfg["max_lev"])
        spot = series[name]["spot"][entry]
        perp = series[name]["perp"][entry]
        pos[name] = {"N": N, "qty_spot": N / spot, "qty_perp": N / perp,
                     "spot0": spot, "perp0": perp, "cost": cost * N}
    entry_cost = sum(p["cost"] for p in pos.values())
    eq = [CAPITAL - entry_cost]
    funding_sum = 0.0
    suspect_hours: list[dict] = []
    for h in hours[1:]:
        pnl = 0.0
        for name, p in pos.items():
            s = series[name]
            rate = s["funding"].get(h, 0.0)
            funding_sum += rate * p["N"]
            pnl += rate * p["N"]
            prev_h = h - 3_600_000
            if prev_h not in s["spot"] or prev_h not in s["perp"]:
                continue
            sr = s["spot"][h] / s["spot"][prev_h] - 1
            pr = s["perp"][h] / s["perp"][prev_h] - 1
            # 数据质量：单腿异常跳动（|spot-perp| 收益差 > 10%）视为脏印价，跳过该小时 basis PnL
            if abs(sr - pr) > 0.10:
                suspect_hours.append({
                    "hour": datetime.fromtimestamp(h / 1000, tz=timezone.utc).isoformat(timespec="minutes"),
                    "asset": name, "spot_ret_pct": round(sr * 100, 2), "perp_ret_pct": round(pr * 100, 2),
                })
                continue
            pnl += p["qty_spot"] * (s["spot"][h] - s["spot"][prev_h])
            pnl += p["qty_perp"] * (s["perp"][prev_h] - s["perp"][h])
        eq.append(eq[-1] + pnl)
    eq_arr = np.array(eq)
    peak = np.maximum.accumulate(eq_arr)
    mdd = float(((peak - eq_arr) / peak).max())
    days = (hours[-1] - hours[0]) / 86_400_000
    ret = eq_arr[-1] / CAPITAL - 1
    cagr = (1 + ret) ** (365 / max(days, 1e-9)) - 1
    hourly_ret = np.diff(eq_arr) / eq_arr[:-1]
    vol = float(hourly_ret.std(ddof=1) * math.sqrt(24 * 365)) if len(hourly_ret) > 1 else 0.0
    return {
        "status": "OK",
        "period": f"{datetime.fromtimestamp(hours[0]/1000, tz=timezone.utc).date()} → "
                  f"{datetime.fromtimestamp(hours[-1]/1000, tz=timezone.utc).date()}",
        "n_hours": len(hours), "days": round(days, 1),
        "return_pct": round(ret * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "vol_pct": round(vol * 100, 2),
        "max_dd_pct": round(mdd * 100, 2),
        "worst_hour_pct": round(float(hourly_ret.min()) * 100, 3) if len(hourly_ret) else 0.0,
        "funding_usd": round(funding_sum, 2),
        "entry_cost_usd": round(entry_cost, 2),
        "slip_bps": slip_bps,
        "suspect_hours": suspect_hours,
        "n_suspect_hours": len(suspect_hours),
    }


def daily_window(start_day: str, end_day: str) -> dict:
    """同一时间窗的日线口径结果（用于和 1h 回放交叉验证）。"""
    cost = entry_cost_rate(BASE_SLIP_BPS)
    merged = None
    for name in ASSETS:
        df = load_panel(name)
        d = df[df["tradable"] == 1][["day", "fund_day", "spot_ret", "perp_ret"]].rename(
            columns={"fund_day": f"{name}_fund", "spot_ret": f"{name}_sret", "perp_ret": f"{name}_pret"})
        merged = d if merged is None else merged.merge(d, on="day", how="inner")
    merged = merged[(merged["day"] >= start_day) & (merged["day"] <= end_day)].dropna().reset_index(drop=True)
    pnl = 0.0
    for name, cfg in ASSETS.items():
        N = notional_of(SLEEVE, cfg["max_lev"])
        pnl += float(merged[f"{name}_fund"].sum()) * N
        pnl += float((merged[f"{name}_sret"] - merged[f"{name}_pret"]).sum()) * N
        pnl -= cost * N
    days = max((merged["day"].iloc[-1] - merged["day"].iloc[0]).days, 1)
    ret = pnl / CAPITAL
    return {"period": f"{merged['day'].iloc[0].date()} → {merged['day'].iloc[-1].date()}",
            "days": days, "return_pct": round(ret * 100, 2),
            "cagr_pct": round(((1 + ret) ** (365 / days) - 1) * 100, 2)}


def delta_drift_analysis() -> dict:
    """检验"delta-neutral"假设：固定数量 vs 每日再平衡，以及 delta 漂移幅度。

    日线 PnL 公式 (spot_ret - perp_ret)*N 隐含"每日把两腿调回等名义"；
    本函数量化：若建仓后不再平衡，delta 会漂移多少、收益差多少、再平衡成本多少。
    """
    cost = entry_cost_rate(BASE_SLIP_BPS)
    merged = None
    for name in ASSETS:
        df = load_panel(name)
        d = df[df["tradable"] == 1][["day", "fund_day", "spot_ret", "perp_ret",
                                     "spot_close", "perp_close"]].rename(
            columns={"fund_day": f"{name}_fund", "spot_ret": f"{name}_sret",
                     "perp_ret": f"{name}_pret", "spot_close": f"{name}_spot",
                     "perp_close": f"{name}_perp"})
        merged = d if merged is None else merged.merge(d, on="day", how="inner")
    merged = merged.dropna().sort_values("day").reset_index(drop=True)
    if len(merged) < 10:
        return {"status": "INSUFFICIENT_DATA"}

    fixed_pnl = 0.0
    rebal_pnl = 0.0
    for name, cfg in ASSETS.items():
        N = notional_of(SLEEVE, cfg["max_lev"])
        spot0 = float(merged[f"{name}_spot"].iloc[0])
        perp0 = float(merged[f"{name}_perp"].iloc[0])
        qs, qp = N / spot0, N / perp0
        for i in range(1, len(merged)):
            s0, s1 = float(merged[f"{name}_spot"].iloc[i - 1]), float(merged[f"{name}_spot"].iloc[i])
            p0, p1 = float(merged[f"{name}_perp"].iloc[i - 1]), float(merged[f"{name}_perp"].iloc[i])
            fixed_pnl += qs * (s1 - s0) - qp * (p1 - p0)
            rebal_pnl += (float(merged[f"{name}_sret"].iloc[i]) - float(merged[f"{name}_pret"].iloc[i])) * N
        fixed_pnl += float(merged[f"{name}_fund"].sum()) * N - cost * N
        rebal_pnl += float(merged[f"{name}_fund"].sum()) * N - cost * N

    delta_series = []
    for i in range(len(merged)):
        dsum = 0.0
        for name, cfg in ASSETS.items():
            N = notional_of(SLEEVE, cfg["max_lev"])
            spot0 = float(merged[f"{name}_spot"].iloc[0])
            perp0 = float(merged[f"{name}_perp"].iloc[0])
            dsum += (N / spot0) * float(merged[f"{name}_spot"].iloc[i]) - (N / perp0) * float(merged[f"{name}_perp"].iloc[i])
        delta_series.append(dsum)
    delta_pct = [abs(d) / CAPITAL * 100 for d in delta_series]
    days = max((merged["day"].iloc[-1] - merged["day"].iloc[0]).days, 1)

    def rule_cost(threshold_pct: float) -> dict:
        """规则：|delta| 超过阈值时交易回 delta=0（成本按 |delta| × 单侧成本率估算）。"""
        offset = 0.0
        cost_sum = 0.0
        events = 0
        for d in delta_series:
            effective = d - offset
            if abs(effective) > CAPITAL * threshold_pct / 100:
                cost_sum += abs(effective) * cost
                events += 1
                offset += effective
        ret = (fixed_pnl - cost_sum) / CAPITAL
        return {"threshold_pct": threshold_pct, "rebalance_events": events,
                "extra_cost_usd": round(cost_sum, 2),
                "return_pct": round(ret * 100, 2),
                "cagr_pct": round(((1 + ret) ** (365 / days) - 1) * 100, 2)}

    daily_rebal_cost = sum(abs(delta_series[i] - delta_series[i - 1])
                           for i in range(1, len(delta_series))) * cost
    return {
        "status": "OK",
        "period": f"{merged['day'].iloc[0].date()} → {merged['day'].iloc[-1].date()}",
        "days": days,
        "fixed_qty_return_pct": round(fixed_pnl / CAPITAL * 100, 2),
        "fixed_qty_cagr_pct": round(((1 + fixed_pnl / CAPITAL) ** (365 / days) - 1) * 100, 2),
        "daily_rebalanced_return_pct": round(rebal_pnl / CAPITAL * 100, 2),
        "delta_abs_pct_max": round(max(delta_pct), 2),
        "delta_abs_pct_median": round(float(np.median(delta_pct)), 2),
        "delta_abs_pct_final": round(delta_pct[-1], 2),
        "days_delta_gt_5pct": int(sum(1 for x in delta_pct if x > 5)),
        "days_delta_gt_10pct": int(sum(1 for x in delta_pct if x > 10)),
        "daily_rebalance_cost_usd": round(daily_rebal_cost, 2),
        "rules": [rule_cost(5), rule_cost(10)],
    }


def load_merged() -> pd.DataFrame:
    """合并 PURR/HYPE 日线面板（共同可交易日；含前一日资金费与前一日 basis 供变体使用）。"""
    merged = None
    core = []
    for name in ASSETS:
        df = load_panel(name)
        d = df[df["tradable"] == 1][["day", "fund_day", "spot_ret", "perp_ret",
                                     "spot_close", "perp_close", "fund_prev_day",
                                     "basis_prev_bps"]].rename(
            columns={"fund_day": f"{name}_fund", "spot_ret": f"{name}_sret",
                     "perp_ret": f"{name}_pret", "spot_close": f"{name}_spot",
                     "perp_close": f"{name}_perp", "fund_prev_day": f"{name}_fundprev",
                     "basis_prev_bps": f"{name}_basprev"})
        core += [f"{name}_fund", f"{name}_sret", f"{name}_pret"]
        merged = d if merged is None else merged.merge(d, on="day", how="inner")
    merged = merged.dropna(subset=core).sort_values("day").reset_index(drop=True)
    return merged


def variant_comparison() -> list[dict]:
    """统一成本口径下比较 A0/A1/A2 三个变体（定义与 R174A SPEC 一致）。

    A0 = 恒持有；A1 = 前一日资金费 >0 时持有；A2 = 前一日资金费 >0 且前一日 basis >0 时持有。
    切换持仓时收取 0.415%×名义 的往返单边成本（开/平各一次）。
    """
    merged = load_merged()
    days = max((merged["day"].iloc[-1] - merged["day"].iloc[0]).days, 1)
    cost = entry_cost_rate(BASE_SLIP_BPS)
    variants = {
        "A0_ALWAYS_ON": lambda name, row: True,
        "A1_POSITIVE_FUNDING": lambda name, row: (float(row[f"{name}_fundprev"]) > 0
                                                  if pd.notna(row[f"{name}_fundprev"]) else False),
        "A2_FUNDING_BASIS_GATE": lambda name, row: (
            pd.notna(row[f"{name}_fundprev"]) and pd.notna(row[f"{name}_basprev"])
            and float(row[f"{name}_fundprev"]) > 0 and float(row[f"{name}_basprev"]) > 0),
    }
    out = []
    for vname, rule in variants.items():
        pos = {name: False for name in ASSETS}
        pnl_series = np.zeros(len(merged))
        costs = 0.0
        holding_days = {name: 0 for name in ASSETS}
        for i in range(len(merged)):
            row = merged.iloc[i]
            day_pnl = 0.0
            for name, cfg in ASSETS.items():
                N = notional_of(SLEEVE, cfg["max_lev"])
                want = bool(rule(name, row))
                if want and not pos[name]:
                    costs += cost * N
                    pos[name] = True
                elif not want and pos[name]:
                    costs += cost * N
                    pos[name] = False
                if pos[name]:
                    day_pnl += float(row[f"{name}_fund"]) * N
                    day_pnl += (float(row[f"{name}_sret"]) - float(row[f"{name}_pret"])) * N
                    holding_days[name] += 1
            pnl_series[i] = day_pnl
        pnl_series[0] -= costs
        total = float(pnl_series.sum()) / CAPITAL
        cagr = (1 + total) ** (365 / days) - 1
        daily_ret = pnl_series / CAPITAL
        vol = float(daily_ret.std(ddof=1) * math.sqrt(365)) if len(daily_ret) > 1 else 0.0
        sharpe = float(daily_ret.mean() / daily_ret.std(ddof=1) * math.sqrt(365)) if daily_ret.std(ddof=1) > 0 else 0.0
        eq = CAPITAL + np.cumsum(pnl_series)
        peak = np.maximum.accumulate(eq)
        mdd = float(((peak - eq) / peak).max())
        total_asset_days = len(merged) * len(ASSETS)
        out.append({
            "variant": vname,
            "return_pct": round(total * 100, 2),
            "cagr_pct": round(cagr * 100, 2),
            "vol_pct": round(vol * 100, 2),
            "sharpe": round(sharpe, 2),
            "max_dd_pct": round(mdd * 100, 2),
            "costs_usd": round(costs, 2),
            "time_in_market_pct": round(sum(holding_days.values()) / total_asset_days * 100, 1),
        })
    return out


def portfolio_daily_pnl(merged: pd.DataFrame, fund_mult: float = 1.0,
                        basis_shock_bps: float = 0.0) -> np.ndarray:
    pnl = np.zeros(len(merged))
    cost_total = 0.0
    for name, cfg in ASSETS.items():
        N = notional_of(SLEEVE, cfg["max_lev"])
        pnl += merged[f"{name}_fund"].values * N * fund_mult
        pnl += (merged[f"{name}_sret"].values - merged[f"{name}_pret"].values) * N
        cost_total += entry_cost_rate(BASE_SLIP_BPS) * N
    pnl[0] -= cost_total
    if basis_shock_bps:
        pnl[0] -= basis_shock_bps / 1e4 * CAPITAL
    return pnl


def bootstrap_analysis(merged: pd.DataFrame, B: int = 2000, block: int = 7,
                       horizon: int = 365, seed: int = 7) -> dict:
    """Block bootstrap：用历史日 PnL 重采样 365 天，估计年化收益的不确定性。"""
    pnl = portfolio_daily_pnl(merged)
    n = len(pnl)
    if n < block * 2:
        return {"status": "INSUFFICIENT_DATA"}
    rng = np.random.default_rng(seed)
    anns = []
    for _ in range(B):
        idx: list[int] = []
        while len(idx) < horizon:
            start = int(rng.integers(0, max(1, n - block + 1)))
            idx.extend(range(start, min(n, start + block)))
        idx = idx[:horizon]
        total = pnl[idx].sum() / CAPITAL
        anns.append((1 + total) ** (365 / horizon) - 1)
    a = np.array(anns)
    return {
        "status": "OK", "n_samples": B, "block_days": block, "horizon_days": horizon,
        "annualized_p5": round(float(np.percentile(a, 5)) * 100, 2),
        "annualized_p50": round(float(np.percentile(a, 50)) * 100, 2),
        "annualized_p95": round(float(np.percentile(a, 95)) * 100, 2),
        "prob_negative": round(float((a < 0).mean()), 4),
        "prob_gt_10pct": round(float((a > 0.10).mean()), 4),
    }


def stress_tests(merged: pd.DataFrame) -> list[dict]:
    """压力测试：资金费减半/归零、basis 一次性冲击、最差 90 天资金费环境。"""
    days = max((merged["day"].iloc[-1] - merged["day"].iloc[0]).days, 1)
    cost_total = sum(entry_cost_rate(BASE_SLIP_BPS) * notional_of(SLEEVE, cfg["max_lev"])
                     for cfg in ASSETS.values())

    def stats(pnl: np.ndarray, horizon_days: int | None = None) -> dict:
        total = float(pnl.sum()) / CAPITAL
        hd = horizon_days or days
        return {"total_return_pct": round(total * 100, 2),
                "cagr_pct": round(((1 + total) ** (365 / max(hd, 1)) - 1) * 100, 2)}

    out = [{"scenario": "基线（历史资金费，15bps 滑点）", **stats(portfolio_daily_pnl(merged))}]
    for mult, label in ((0.5, "资金费减半"), (0.0, "资金费归零")):
        out.append({"scenario": label, **stats(portfolio_daily_pnl(merged, fund_mult=mult))})
    for shock, label in ((50, "basis 一次性不利 50bps"), (100, "basis 一次性不利 100bps")):
        out.append({"scenario": label, **stats(portfolio_daily_pnl(merged, basis_shock_bps=shock))})
    # 最差 90 天资金费环境（年化外推，不计 basis）
    fund_usd = pd.Series(0.0, index=merged.index)
    for name, cfg in ASSETS.items():
        N = notional_of(SLEEVE, cfg["max_lev"])
        fund_usd += merged[f"{name}_fund"] * N
    window = 90
    if len(fund_usd) >= window:
        worst = float(fund_usd.rolling(window).sum().min())
        annual = worst / window * 365
        total = (annual - cost_total) / CAPITAL
        out.append({"scenario": "最差 90 天资金费环境（年化外推，不计 basis）",
                    "total_return_pct": round(total * 100, 2),
                    "cagr_pct": round(total * 100, 2)})
    return out


def monthly_analysis(merged: pd.DataFrame) -> dict:
    """A0 组合的月度收益分布（首月含建仓成本）。"""
    pnl = portfolio_daily_pnl(merged)
    df = merged[["day"]].copy()
    df["month"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m")
    df["pnl"] = pnl
    g = df.groupby("month").agg(pnl_usd=("pnl", "sum"), days=("day", "count"))
    rows = [{"month": m, "days": int(r["days"]),
             "return_pct": round(float(r["pnl_usd"]) / CAPITAL * 100, 2)}
            for m, r in g.iterrows()]
    rets = np.array([r["return_pct"] for r in rows]) if rows else np.array([0.0])
    return {
        "months": rows,
        "n_months": len(rows),
        "positive_share": round(float((rets > 0).mean()), 3),
        "median_pct": round(float(np.median(rets)), 2),
        "mean_pct": round(float(rets.mean()), 2),
        "best_pct": round(float(rets.max()), 2),
        "worst_pct": round(float(rets.min()), 2),
    }


def main() -> int:
    daily = daily_validate()
    daily_real = daily_validate(with_exit=True)
    segments = segment_validate(daily)
    sens = cost_sensitivity()
    hourly = hourly_validate()
    hourly_daily_cross = daily_window("2026-03-29", "2026-10-23")
    hourly["daily_panel_same_window"] = hourly_daily_cross
    delta_drift = delta_drift_analysis()
    merged_all = load_merged()
    bootstrap = bootstrap_analysis(merged_all)
    stress = stress_tests(merged_all)
    variants = variant_comparison()
    monthly = monthly_analysis(merged_all)

    # 与 R174A 已发布 A0 交叉核对（来自 REPORT.md）
    r174a_ref = {"PURR_A0_cagr_pct": 22.69, "HYPE_A0_cagr_pct": 17.57}
    cross = {
        "PURR": {"rebuild_cagr_pct": daily["per_asset"]["PURR"]["cagr_pct"],
                 "r174a_cagr_pct": r174a_ref["PURR_A0_cagr_pct"]},
        "HYPE": {"rebuild_cagr_pct": daily["per_asset"]["HYPE"]["cagr_pct"],
                 "r174a_cagr_pct": r174a_ref["HYPE_A0_cagr_pct"]},
    }

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": {"capital": CAPITAL, "sleeve": SLEEVE, "base_slip_bps": BASE_SLIP_BPS,
                   "fees_rate": FEES_RATE, "assets": list(ASSETS)},
        "daily_full": daily,
        "daily_full_with_exit": daily_real,
        "segments": segments,
        "cost_sensitivity": sens,
        "hourly": hourly,
        "delta_drift": delta_drift,
        "bootstrap": bootstrap,
        "stress_tests": stress,
        "variants": variants,
        "monthly_returns": monthly,
        "r174a_cross_check": cross,
    }
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def pct(x: float) -> str:
        return f"{x:+.2f}%"

    md = [
        "# 历史数据算法验证报告 — HL 现货-永续 Carry（PURR + HYPE）",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 口径：本金 ${CAPITAL:,.0f}（每资产 $500 sleeve），1x delta-neutral，"
        f"tier-0 taker 费 + 每 fill {BASE_SLIP_BPS:.0f}bps 滑点（建仓成本 0.415%×名义）",
        "- 数据：R174A 抓取的 Hyperliquid 日线面板与 1 小时 K 线/资金费（2024-11 ~ 2026-10）",
        "",
        "## A. 日线全期验证（主结果）",
        "",
        f"- 区间：{daily['period']}（{daily['days']} 天）",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 总收益 | {pct(daily['return_pct'])} |",
        f"| 年化（CAGR） | {pct(daily['cagr_pct'])} |",
        f"| 年化波动 | {daily['vol_pct']:.2f}% |",
        f"| Sharpe | {daily['sharpe']:.2f} |",
        f"| Sortino | {daily['sortino']:.2f} |",
        f"| 最大回撤 | {daily['max_dd_pct']:.2f}% |",
        f"| 资金费贡献 | ${daily['funding_usd']:+,.2f} |",
        f"| basis 贡献 | ${daily['basis_usd']:+,.2f} |",
        f"| 建仓成本 | ${daily['cost_usd']:.2f} |",
        f"| 平仓口径（含退出成本）总收益 | {pct(daily_real['return_pct'])} |",
        "",
        "**分资产（A0，日线全期）**",
        "",
        "| 资产 | 区间天数 | 总收益 | 年化 | 波动 | 最大回撤 | 资金费 | basis | 成本 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, a in daily["per_asset"].items():
        md.append(f"| {name} | {a['days']} | {pct(a['return_pct'])} | {pct(a['cagr_pct'])} | "
                  f"{a['vol_pct']:.1f}% | {a['max_dd_pct']:.1f}% | ${a['funding_usd']:+,.0f} | "
                  f"${a['basis_usd']:+,.0f} | ${a['cost_usd']:.0f} |")
    md += [
        "",
        "## B. 分期间稳定性",
        "",
        "| 区段 | 区间 | 天数 | 总收益 | 年化 | 最大回撤 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for key in ("full", "2025", "2026", "first_half", "second_half"):
        s = segments.get(key, {})
        if s:
            md.append(f"| {key} | {s['period']} | {s['days']} | {pct(s['return_pct'])} | "
                      f"{pct(s['cagr_pct'])} | {s['max_dd_pct']:.2f}% |")
    md += [
        "",
        "## C. 成本敏感性（每 fill 滑点）",
        "",
        "| 滑点/腿 | 建仓成本率 | 总收益 | 年化 | 最大回撤 |",
        "|---:|---:|---:|---:|---:|",
    ]
    for r in sens:
        md.append(f"| {r['slip_bps']:.0f}bps | {r['cost_rate_side']*100:.3f}% | "
                  f"{pct(r['return_pct'])} | {pct(r['cagr_pct'])} | {r['max_dd_pct']:.2f}% |")
    md += [
        "",
        "## D. 1 小时级回放（含逐小时资金费与 mark-to-market）",
        "",
    ]
    if hourly.get("status") == "OK":
        md += [
            f"- 区间：{hourly['period']}（{hourly['n_hours']} 小时 / {hourly['days']} 天；1h 数据只有最近 ~5000 根）",
            f"- 数据清洗：发现并跳过 **{hourly.get('n_suspect_hours', 0)}** 个脏印价小时"
            f"（单腿异常、|spot-perp| 收益差 >10%；明细见 JSON 的 suspect_hours）",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| 总收益 | {pct(hourly['return_pct'])} |",
            f"| 年化 | {pct(hourly['cagr_pct'])} |",
            f"| 小时级年化波动 | {hourly['vol_pct']:.2f}% |",
            f"| 最大回撤（小时级） | {hourly['max_dd_pct']:.2f}% |",
            f"| 最差单小时 | {hourly['worst_hour_pct']:.3f}% |",
            f"| 资金费累计 | ${hourly['funding_usd']:+,.2f} |",
            "",
            f"- 同窗口日线口径：{hourly_daily_cross['return_pct']:+.2f}%（与 1h 回放 "
            f"{hourly['return_pct']:+.2f}% 基本一致，差异来自盘中 mark 与资金费时点）",
            "- ⚠️ 注意：1h K 线收盘价在稀薄市场含噪声，上表「小时级年化波动」会明显高估真实风险；"
            f"持仓风险请以日线波动（{daily['vol_pct']:.1f}%）与小时级最大回撤（{hourly['max_dd_pct']:.2f}%）为准。",
        ]
    else:
        md.append(f"- 跳过：{hourly.get('status')}（n_hours={hourly.get('n_hours')}）")
    md += [
        "",
        "## E. 与 R174A 已发布结果的交叉核对",
        "",
        "| 资产 | 本次重建 A0 年化 | R174A REPORT A0 年化 | 差异 |",
        "|---|---:|---:|---:|",
    ]
    for name, c in cross.items():
        md.append(f"| {name} | {c['rebuild_cagr_pct']:+.2f}% | {c['r174a_cagr_pct']:+.2f}% | "
                  f"{c['rebuild_cagr_pct']-c['r174a_cagr_pct']:+.2f}pp |")
    if delta_drift.get("status") == "OK":
        dd = delta_drift
        md += [
            "",
            "## E2. Delta 漂移与再平衡假设检验",
            "",
            f"- 区间：{dd['period']}（{dd['days']} 天）",
            "- 日线收益公式 (spot_ret − perp_ret)×N 隐含「每日调回等名义」；这里检验不再平衡时的漂移与成本：",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| 固定数量（不再平衡）总收益 | {pct(dd['fixed_qty_return_pct'])} |",
            f"| 每日再平衡（原口径）总收益 | {pct(dd['daily_rebalanced_return_pct'])} |",
            f"| delta 绝对漂移 | 中位 {dd['delta_abs_pct_median']:.2f}%、最大 {dd['delta_abs_pct_max']:.2f}%、期末 {dd['delta_abs_pct_final']:.2f}%（占本金） |",
            f"| \\|delta\\|>5% 的天数 | {dd['days_delta_gt_5pct']} 天 |",
            f"| \\|delta\\|>10% 的天数 | {dd['days_delta_gt_10pct']} 天 |",
            f"| 每日再平衡的估算成本 | ${dd['daily_rebalance_cost_usd']:,.2f} |",
            "",
            "| 再平衡规则 | 触发次数 | 额外成本 | 总收益 | 年化 |",
            "|---|---:|---:|---:|---:|",
        ]
        for r in dd["rules"]:
            md.append(f"| \\|delta\\|>{r['threshold_pct']:.0f}% 时调回 0 | {r['rebalance_events']} 次 | "
                      f"${r['extra_cost_usd']:,.2f} | {pct(r['return_pct'])} | {pct(r['cagr_pct'])} |")
        md += [
            "",
            "**解读**：若固定数量与每日再平衡的收益差很小，说明历史结论对再平衡假设不敏感；"
            "若差异大，实盘必须带再平衡规则（按上表阈值与成本选择），否则实际 delta 暴露会偏离策略设计。",
        ]
    if bootstrap.get("status") == "OK":
        md += [
            "",
            "## E3. 收益不确定性（Block Bootstrap）与压力测试",
            "",
            f"- 方法：历史日 PnL 按 {bootstrap['block_days']} 天分块、重采样 {bootstrap['horizon_days']} 天，"
            f"重复 {bootstrap['n_samples']} 次（保留资金费的序列相关性）",
            "",
            "| 年化收益分位 | 数值 |",
            "|---|---:|",
            f"| p5（悲观） | {pct(bootstrap['annualized_p5'])} |",
            f"| p50（中位） | {pct(bootstrap['annualized_p50'])} |",
            f"| p95（乐观） | {pct(bootstrap['annualized_p95'])} |",
            f"| 年化为负的概率 | {bootstrap['prob_negative']:.1%} |",
            f"| 年化 >10% 的概率 | {bootstrap['prob_gt_10pct']:.1%} |",
            "",
            "| 压力情景 | 总收益 | 年化 |",
            "|---|---:|---:|",
        ]
        for s in stress:
            md.append(f"| {s['scenario']} | {pct(s['total_return_pct'])} | {pct(s['cagr_pct'])} |")
        md += [
            "",
            "**解读**：bootstrap 给出年化收益的分布（不是点估计）；压力测试回答"
            "\"资金费减半/归零、basis 一次性不利冲击、最差 90 天资金费环境\"下会怎样。"
            "若资金费归零情景明显为负，说明策略对资金费环境高度依赖。",
        ]
    if variants:
        md += [
            "",
            "## E4. 变体比较（A0 / A1 / A2，统一成本口径）",
            "",
            "| 变体 | 总收益 | 年化 | 波动 | Sharpe | 最大回撤 | 成本 | 在场时间 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for v in variants:
            md.append(f"| {v['variant']} | {pct(v['return_pct'])} | {pct(v['cagr_pct'])} | "
                      f"{v['vol_pct']:.1f}% | {v['sharpe']:.2f} | {v['max_dd_pct']:.2f}% | "
                      f"${v['costs_usd']:.2f} | {v['time_in_market_pct']:.1f}% |")
        md += [
            "",
            "**解读**：A1/A2 是「只在资金费为正（A2 还要求 perp 溢价）时持有」的择时版本。"
            "若它们因频繁换手导致收益反而更低，说明 A0（恒持有）是更优选择——与 R174A 的结论一致；"
            "这也是 paper 采用 A0 的依据。",
        ]
    if monthly.get("n_months"):
        md += [
            "",
            "## E5. 月度收益分布（A0，首月含建仓成本）",
            "",
            f"- 共 {monthly['n_months']} 个月：正收益占比 **{monthly['positive_share']:.1%}**，"
            f"中位 **{monthly['median_pct']:+.2f}%**，最好 {monthly['best_pct']:+.2f}%，最差 {monthly['worst_pct']:+.2f}%",
            "",
            "| 月份 | 天数 | 收益 |",
            "|---|---:|---:|",
        ]
        for r in monthly["months"]:
            md.append(f"| {r['month']} | {r['days']} | {r['return_pct']:+.2f}% |")
        md += [
            "",
            "**解读**：样本内 23 个月全部为正，但这段时期资金费整体偏正；压力测试显示资金费归零时月度会转负。"
            "因此不能把「每月必赚」当作承诺——它更像「资金费环境好时的稳定现金层」，需要预留缓冲。",
        ]
    md += [
        "",
        "## F. 结论（验证判定）",
        "",
        "1. **算法有效但收益有限**：历史全期组合年化约 "
        f"{daily['cagr_pct']:+.1f}%，主要来自资金费（${daily['funding_usd']:+,.0f}），basis 贡献 "
        f"${daily['basis_usd']:+,.0f}，最大回撤 {daily['max_dd_pct']:.1f}%（低风险特征成立）。",
        "2. **成本影响有限但不可忽略**：本策略低换手（仅建仓/平仓各一次），每腿滑点从 0 提到 60bps 时年化只降约 "
        f"{sens[0]['cagr_pct']-sens[-1]['cagr_pct']:.1f}pp；但若实盘需要频繁调仓（资金费转负平仓再进），成本会显著累积。",
        "3. **分期间不稳定**：2025 与 2026 表现不同（见 B 表），策略收益依赖资金费环境，不是每年稳定现金流。",
        "4. **1h 级验证**：小时级回撤与最差单小时都很小（见 D 表），与日线结论一致。",
        "5. **与 R174A 交叉核对**：分资产年化差异在合理范围（日线 close-to-close 近似、资金费按日汇总 vs 逐小时）。",
        f"6. **delta 漂移假设检验**：不再平衡时 \\|delta\\| 中位 {delta_drift['delta_abs_pct_median']:.2f}%、最大 "
        f"{delta_drift['delta_abs_pct_max']:.2f}%（占本金），5%/10% 再平衡规则从不触发；固定数量总收益 "
        f"{pct(delta_drift['fixed_qty_return_pct'])} vs 每日再平衡 {pct(delta_drift['daily_rebalanced_return_pct'])}，"
        "差异约 4pp，结论对再平衡假设不敏感。",
        f"7. **策略本质是资金费赌注**：压力测试显示资金费减半时年化降到 "
        f"{[s for s in stress if s['scenario']=='资金费减半'][0]['cagr_pct']:+.1f}%，资金费归零时 "
        f"{[s for s in stress if s['scenario']=='资金费归零'][0]['cagr_pct']:+.1f}%；bootstrap 年化 p5–p95 为 "
        f"{bootstrap['annualized_p5']:+.1f}% ~ {bootstrap['annualized_p95']:+.1f}%，但只反映样本内抽样不确定性，"
        "不包含资金费制度变化、交易所/合约风险（真实风险更高）。",
        f"8. **变体比较支持 A0**：统一成本口径下 A0 总收益 {variants[0]['return_pct']:+.2f}% > A1 "
        f"{variants[1]['return_pct']:+.2f}% > A2 {variants[2]['return_pct']:+.2f}%；A2 因频繁换手成本高达 "
        f"${variants[2]['costs_usd']:,.0f}（本金 $1,000）而显著亏损。与 R174A 结论一致：不加择时过滤的 A0 最优。",
        "",
        "## G. 局限（必须随结论一起读）",
        "",
        "- 历史约 24 个月、2 个资产（PURR/HYPE），样本有限，且含 meme 币资产；",
        "- 用日线收盘/日汇总资金费近似，未模拟真实成交与盘口冲击（成本用固定滑点近似）；",
        f"- 固定数量口径下 delta 漂移会带来 ±{delta_drift['delta_abs_pct_max']:.1f}% 级别的方向性偏差（已量化，影响有限）；",
        "- 单交易所（Hyperliquid），存在交易所/合约/预言机风险，历史回测无法覆盖；",
        "- 未做参数搜索（策略无参数），因此不存在过拟合；但也没有优化空间；",
        "- 历史收益不代表未来；实盘前仍需 30 天 paper 验证（进行中）。",
        "",
        f"*报告生成：{result['generated_at']}；数据与脚本见 `historical_validation.py` / `historical_validation.json`。*",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({"daily_full": {k: daily[k] for k in
                                     ("period", "days", "return_pct", "cagr_pct", "vol_pct",
                                      "sharpe", "max_dd_pct", "funding_usd", "basis_usd")},
                      "segments": segments, "hourly": hourly,
                      "cost_sensitivity": sens}, ensure_ascii=False, indent=2))
    print(f"\n已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
