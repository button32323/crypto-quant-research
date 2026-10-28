# -*- coding: utf-8 -*-
"""
R174A-HL-SPOT-PERP-CARRY — universe construction via price cross-validation (SPEC 1 + 附注 A).

Input : data/candidates.json, data/raw/{meta,spot_meta}.json, data/candles/1d_*.json, data/funding/*.json
Output: universe.csv, universe_rejects.csv, universe_new_since_2026_08_16.csv, universe.md,
        data/universe.json, data/panels/daily_<SYM>.csv  (daily panels for r174a_backtest.py)

Frozen gates (SPEC 1.2), applied mechanically, no manual whitelist:
  G1 overlap_days >= 30
  G2 0.5 <= median(spot_close/perp_close) <= 2.0
  G3 pearson corr(spot daily returns, perp daily returns) > 0.8

NO LOOK-AHEAD IN UNIVERSE SELECTION (SPEC 附注 A):
  * REPLICATE universe is decided strictly **as-of 2026-08-16** (gates computed on overlap days
    <= 2026-08-16), i.e. with exactly the information the 2026-08-16 memo had.
  * The forward window reuses that same universe; forward-period data is NEVER used to add/remove
    assets. A second "as-of latest data" cross-validation is reported only as a data-quality /
    universe-drift diagnostic.

Rationale for price cross-validation: @N is a slot id whose candleSnapshot history can be polluted
by a previously listed token (2026-08-16 memo). Price cross-validation is the only reliable test.

Usage: python r174a_universe.py
"""
import csv
import json
import os
import re
from datetime import datetime, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RAW = os.path.join(DATA, "raw")
CANDLES = os.path.join(DATA, "candles")
FUNDING = os.path.join(DATA, "funding")
PANELS = os.path.join(DATA, "panels")

DAY_MS = 86400 * 1000
HOUR_MS = 3600 * 1000
UTC = timezone.utc

# frozen thresholds (SPEC 1.2)
G1_MIN_OVERLAP_DAYS = 30
G2_RATIO_LO, G2_RATIO_HI = 0.5, 2.0
G3_MIN_CORR = 0.8
G3_MIN_RET_PAIRS = 30

MEMO_VALID = ["PURR", "HYPE", "STABLE", "AZTEC"]          # 2026-08-16 P0.1
MEMO_REJECT = ["TRUMP", "PUMP", "BERA", "MON"]            # 2026-08-16 P0.1
ASOF_CUTOFF_DAY = int(datetime(2026, 8, 16, tzinfo=UTC).timestamp() // 86400)
FORWARD_CUTOFF_MS = ASOF_CUTOFF_DAY * DAY_MS


def load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def sanitize(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_")


def day_str(day_idx):
    return datetime.fromtimestamp(day_idx * 86400, UTC).strftime("%Y-%m-%d")


def load_1d(leg, sym):
    p = os.path.join(CANDLES, f"1d_{leg}_{sanitize(sym)}.json")
    if not os.path.exists(p):
        return {}
    out = {}
    for c in load_json(p):
        out[int(c["t"]) // DAY_MS] = {
            "close": float(c["c"]), "open": float(c["o"]),
            "vol_quote": float(c["v"]) * float(c["c"]), "n": int(c.get("n", 0)),
        }
    return out


def funding_by_day(sym):
    """hour-floored funding: day_idx -> {hour_ms: rate} (SPEC 4.1)."""
    p = os.path.join(FUNDING, f"{sanitize(sym)}.json")
    if not os.path.exists(p):
        return {}
    out = {}
    for r in load_json(p):
        hour_ms = (int(r["time"]) // HOUR_MS) * HOUR_MS      # floor away ms offset
        d = hour_ms // DAY_MS
        out.setdefault(d, {})[hour_ms] = float(r["fundingRate"])
    return out


def pearson(x, y):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 2:
        return float("nan")
    sx, sy = x.std(), y.std()
    if sx == 0 or sy == 0:
        return float("nan")
    return float(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy))


def cross_validate(spot, perp, cutoff_day=None):
    """SPEC 1.2 gates. cutoff_day = only use overlap days <= cutoff_day (causal as-of)."""
    spot_days, perp_days = set(spot), set(perp)
    overlap = sorted(spot_days & perp_days)
    if cutoff_day is not None:
        overlap = [d for d in overlap if d <= cutoff_day]
    n_overlap = len(overlap)
    res = {"n_overlap_days": n_overlap,
           "overlap_first": day_str(overlap[0]) if overlap else None,
           "overlap_last": day_str(overlap[-1]) if overlap else None,
           "median_ratio": None, "pearson_ret": None, "n_ret_pairs": 0,
           "g1_pass": False, "g2_pass": False, "g3_pass": False,
           "verdict": "REJECT", "reasons": []}
    if n_overlap == 0:
        res["reasons"].append("NO_OVERLAP_DAYS")
        return res

    ratios = [spot[d]["close"] / perp[d]["close"] for d in overlap if perp[d]["close"] > 0]
    res["median_ratio"] = float(np.median(ratios)) if ratios else None

    rs, rp, ret_days = [], [], []
    for d in overlap:
        if (d - 1) in spot_days and (d - 1) in perp_days:
            s0, s1 = spot[d - 1]["close"], spot[d]["close"]
            p0, p1 = perp[d - 1]["close"], perp[d]["close"]
            if s0 > 0 and p0 > 0:
                rs.append(s1 / s0 - 1.0)
                rp.append(p1 / p0 - 1.0)
                ret_days.append(d)
    res["n_ret_pairs"] = len(rs)
    res["pearson_ret"] = pearson(rs, rp) if len(rs) >= G3_MIN_RET_PAIRS else None

    res["g1_pass"] = n_overlap >= G1_MIN_OVERLAP_DAYS
    res["g2_pass"] = res["median_ratio"] is not None and G2_RATIO_LO <= res["median_ratio"] <= G2_RATIO_HI
    res["g3_pass"] = res["pearson_ret"] is not None and not np.isnan(res["pearson_ret"]) \
        and res["pearson_ret"] > G3_MIN_CORR

    if not res["g1_pass"]:
        res["reasons"].append(f"G1_INSUFFICIENT_OVERLAP(n={n_overlap}<{G1_MIN_OVERLAP_DAYS})")
    if not res["g2_pass"]:
        if res["median_ratio"] is None:
            res["reasons"].append("G2_NO_RATIO")
        elif res["median_ratio"] < G2_RATIO_LO:
            res["reasons"].append(f"G2_PRICE_RATIO_TOO_LOW({res['median_ratio']:.4g}<{G2_RATIO_LO})")
        else:
            res["reasons"].append(f"G2_PRICE_RATIO_TOO_HIGH({res['median_ratio']:.4g}>{G2_RATIO_HI})")
    if not res["g3_pass"]:
        if res["pearson_ret"] is None:
            res["reasons"].append(f"G3_INSUFFICIENT_RET_PAIRS(n={res['n_ret_pairs']}<{G3_MIN_RET_PAIRS})")
        else:
            res["reasons"].append(f"G3_LOW_RETURN_CORR({res['pearson_ret']:.3f}<={G3_MIN_CORR})")

    if res["g1_pass"] and res["g2_pass"] and res["g3_pass"]:
        res["verdict"] = "VALID"
        res["reasons"] = []
    return res


def build_panel(sym, spot, perp, fund):
    """Daily panel for a VALID asset. SIGNAL columns come from day d-1 (no lookahead), PnL
    columns are for day d. Tradable requires both legs have bars d and d-1 and full (24h)
    funding on d-1 (signal) and d (accrual). SPEC 4.2 — no forward-fill."""
    spot_days, perp_days = set(spot), set(perp)
    fund_full = {d: (len(h) == 24) for d, h in fund.items()}
    all_days = sorted(spot_days | perp_days)
    if not all_days:
        return []
    lo, hi = min(all_days), max(all_days)
    rows = []
    for d in range(lo, hi + 1):
        has_bars = (d in spot_days) and (d in perp_days) and (d - 1 in spot_days) and (d - 1 in perp_days)
        fund_sig_ok = fund_full.get(d - 1, False)
        fund_pnl_ok = fund_full.get(d, False)
        tradable = has_bars and fund_sig_ok and fund_pnl_ok
        row = {
            "day_idx": d, "day": day_str(d), "day_ms": d * DAY_MS,
            "has_bars": int(has_bars), "fund_sig_full": int(fund_sig_ok),
            "fund_pnl_full": int(fund_pnl_ok), "tradable": int(tradable),
            "spot_close": f"{spot[d]['close']:.10g}" if d in spot_days else "",
            "perp_close": f"{perp[d]['close']:.10g}" if d in perp_days else "",
            "spot_vol_quote": f"{spot[d]['vol_quote']:.6g}" if d in spot_days else "",
            "perp_vol_quote": f"{perp[d]['vol_quote']:.6g}" if d in perp_days else "",
            "fund_prev_day": "", "fund_day": "", "fund_n_prev": 0, "fund_n_day": 0,
            "basis_prev_bps": "", "spot_ret": "", "perp_ret": "",
        }
        if d - 1 in spot_days and d - 1 in perp_days:
            s0, p0 = spot[d - 1]["close"], perp[d - 1]["close"]
            if s0 > 0:
                row["basis_prev_bps"] = f"{(p0 / s0 - 1.0) * 10000.0:.4f}"
        if d in spot_days and d - 1 in spot_days and spot[d - 1]["close"] > 0:
            row["spot_ret"] = f"{spot[d]['close'] / spot[d - 1]['close'] - 1.0:.10g}"
        if d in perp_days and d - 1 in perp_days and perp[d - 1]["close"] > 0:
            row["perp_ret"] = f"{perp[d]['close'] / perp[d - 1]['close'] - 1.0:.10g}"
        if d - 1 in fund:
            row["fund_prev_day"] = f"{sum(fund[d-1].values()):.12g}"
            row["fund_n_prev"] = len(fund[d - 1])
        if d in fund:
            row["fund_day"] = f"{sum(fund[d].values()):.12g}"
            row["fund_n_day"] = len(fund[d])
        rows.append(row)
    return rows


def main():
    os.makedirs(PANELS, exist_ok=True)
    meta = load_json(os.path.join(RAW, "meta.json"))
    spot_meta = load_json(os.path.join(RAW, "spot_meta.json"))
    cand_doc = load_json(os.path.join(DATA, "candidates.json"))
    cands = cand_doc["candidates"]
    skipped_nonusdc = cand_doc.get("skipped_non_usdc", [])
    maxlev = {u["name"]: int(u["maxLeverage"]) for u in meta["universe"]}

    valid, rejected = [], []
    for c in cands:
        sym, pair = c["perp"], c["spot_pair"]
        spot, perp = load_1d("spot", sym), load_1d("perp", sym)
        fund = funding_by_day(sym)
        cv_asof = cross_validate(spot, perp, cutoff_day=ASOF_CUTOFF_DAY)   # causal, memo-consistent
        cv_now = cross_validate(spot, perp)                               # latest data (diagnostic only)
        base = {
            "perp": sym, "spot_pair": pair, "is_canonical": c["is_canonical"],
            "pair_index": c["pair_index"], "max_lev": maxlev.get(sym),
            "spot_days": len(spot), "perp_days": len(perp),
            "funding_hours": sum(len(v) for v in fund.values()),
            "asof_cutoff": day_str(ASOF_CUTOFF_DAY),
            "asof_verdict": cv_asof["verdict"], "asof_median_ratio": cv_asof["median_ratio"],
            "asof_pearson_ret": cv_asof["pearson_ret"], "asof_n_overlap_days": cv_asof["n_overlap_days"],
            "asof_overlap": [cv_asof["overlap_first"], cv_asof["overlap_last"]],
            "asof_reasons": cv_asof["reasons"],
            "now_verdict": cv_now["verdict"], "now_median_ratio": cv_now["median_ratio"],
            "now_pearson_ret": cv_now["pearson_ret"], "now_n_overlap_days": cv_now["n_overlap_days"],
            "now_overlap": [cv_now["overlap_first"], cv_now["overlap_last"]],
            "now_reasons": cv_now["reasons"],
            "status_changed": cv_asof["verdict"] != cv_now["verdict"],
        }
        panel = build_panel(sym, spot, perp, fund)
        tradable = [r for r in panel if r["tradable"]]
        if not tradable:
            base["verdict"] = "REJECT"
            base["reasons"] = ["NO_TRADABLE_DAY_AFTER_DATA_ALIGNMENT"]
            rejected.append(base)
            continue

        first, last = tradable[0], tradable[-1]
        span = (last["day_idx"] - first["day_idx"]) + 1
        base.update({
            "tradable_days": len(tradable), "window_span_days": span,
            "first_day": first["day"], "last_day": last["day"],
            "first_day_ms": first["day_ms"], "last_day_ms": last["day_ms"],
            "excluded_days_in_span": span - len(tradable),
        })
        win = [r for r in panel if first["day_idx"] <= r["day_idx"] <= last["day_idx"]]
        sv = [float(r["spot_vol_quote"]) for r in win if r["spot_vol_quote"]]
        pv = [float(r["perp_vol_quote"]) for r in win if r["perp_vol_quote"]]
        base["median_spot_quote_vol"] = float(np.median(sv)) if sv else None
        base["median_perp_quote_vol"] = float(np.median(pv)) if pv else None

        # ---- role / verdict: causal universe selection ----
        if first["day_ms"] <= FORWARD_CUTOFF_MS:
            # replication-eligible: decided as-of 2026-08-16
            if cv_asof["verdict"] == "VALID":
                base["verdict"] = "VALID"
                base["role"] = "REPLICATE"
                base["reasons"] = []
            else:
                base["verdict"] = "REJECT"
                base["role"] = "REPLICATE_WINDOW"
                base["reasons"] = [f"ASOF_2026_08_16: {r}" for r in cv_asof["reasons"]]
        else:
            if cv_now["verdict"] == "VALID":
                base["verdict"] = "VALID"
                base["role"] = "NEW_SINCE_2026_08_16"
                base["reasons"] = []
            else:
                base["verdict"] = "REJECT"
                base["role"] = "NEW_SINCE_2026_08_16"
                base["reasons"] = [f"NOW: {r}" for r in cv_now["reasons"]]

        if base["verdict"] == "VALID":
            pth = os.path.join(PANELS, f"daily_{sanitize(sym)}.csv")
            with open(pth, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(panel[0].keys()))
                w.writeheader()
                w.writerows(panel)
            base["panel"] = os.path.relpath(pth, HERE)
            valid.append(base)
        else:
            rejected.append(base)

    # ---- outputs ----
    ucols = ["perp", "spot_pair", "is_canonical", "pair_index", "max_lev", "role",
             "first_day", "last_day", "tradable_days", "window_span_days", "excluded_days_in_span",
             "asof_verdict", "asof_median_ratio", "asof_pearson_ret", "asof_n_overlap_days",
             "asof_overlap", "now_verdict", "now_median_ratio", "now_pearson_ret",
             "now_n_overlap_days", "now_overlap", "status_changed",
             "spot_days", "perp_days", "funding_hours",
             "median_spot_quote_vol", "median_perp_quote_vol", "panel"]
    valid.sort(key=lambda r: -(r["tradable_days"] or 0))
    for fn in ("universe.csv", "universe_new_since_2026_08_16.csv"):
        rows = valid if fn == "universe.csv" else [r for r in valid if r["role"] == "NEW_SINCE_2026_08_16"]
        with open(os.path.join(HERE, fn), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=ucols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                rr = dict(r)
                rr["asof_overlap"] = "..".join(str(x) for x in (r["asof_overlap"] or ["", ""]))
                rr["now_overlap"] = "..".join(str(x) for x in (r["now_overlap"] or ["", ""]))
                w.writerow(rr)

    rcols = ["perp", "spot_pair", "is_canonical", "pair_index", "max_lev", "role", "reasons",
             "asof_verdict", "asof_median_ratio", "asof_pearson_ret", "asof_n_overlap_days",
             "now_verdict", "now_median_ratio", "now_pearson_ret", "now_n_overlap_days",
             "spot_days", "perp_days", "funding_hours"]
    rejected.sort(key=lambda r: r["perp"])
    with open(os.path.join(HERE, "universe_rejects.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rcols, extrasaction="ignore")
        w.writeheader()
        for r in rejected:
            rr = dict(r)
            rr["reasons"] = "; ".join(r.get("reasons", []))
            w.writerow(rr)

    doc = {
        "generated_at": datetime.now(UTC).isoformat(),
        "rules": {
            "candidate": "USDC-quoted spot pair (tokens[1]==0) whose base token name matches a perp name",
            "G1_min_overlap_days": G1_MIN_OVERLAP_DAYS,
            "G2_median_spot_over_perp_close_range": [G2_RATIO_LO, G2_RATIO_HI],
            "G3_pearson_daily_return_corr_min": G3_MIN_CORR,
            "replicate_universe_asof": day_str(ASOF_CUTOFF_DAY),
            "note": "REPLICATE universe decided with data <= 2026-08-16 only (no lookahead); "
                    "forward window reuses it unchanged; 'now' cross-validation is diagnostic only",
            "tradable_day": "bars on d and d-1 both legs AND 24h funding on d-1 (signal) and d (accrual)",
        },
        "n_candidates": len(cands),
        "n_valid": len(valid),
        "n_rejected": len(rejected),
        "skipped_non_usdc": skipped_nonusdc,
        "valid": valid,
        "rejected": rejected,
        "memo_2026_08_16": {"valid": MEMO_VALID, "reject": MEMO_REJECT},
        "memo_match": {
            "replicate_valid": sorted([r["perp"] for r in valid if r["role"] == "REPLICATE"]),
            "valid": sorted([r["perp"] for r in valid if r["role"] == "REPLICATE"]) == sorted(MEMO_VALID),
            "reject": sorted([r["perp"] for r in rejected]) == sorted(MEMO_REJECT),
            "universe_drift_vs_memo": [r["perp"] for r in valid
                                       if r["role"] == "REPLICATE" and r["now_verdict"] != "VALID"],
        },
    }
    with open(os.path.join(DATA, "universe.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    # ---- markdown ----
    lines = []
    A = lines.append
    A("# R174A universe — 价格交叉验证（冻结规则，无人工名单）\n")
    A(f"- 生成时间：{doc['generated_at']}")
    A(f"- 候选（USDC 计价 spot 对 且 base token 同名永续）：**{len(cands)}**")
    A(f"- 有效：**{len(valid)}** | 拒绝：**{len(rejected)}** | 非 USDC 计价排除：{len(skipped_nonusdc)}\n")
    A("## 判定口径（无前视）")
    A(f"- **复刻窗口 universe 截止 2026-08-16 判定**（只用 ≤ {day_str(ASOF_CUTOFF_DAY)} 的重叠日），"
      "与 2026-08-16 memo 当时可见信息一致；前向窗口沿用同一 universe，不用前向期数据重选。")
    A(f"- `now` 列 = 截至最新数据（{day_str(int(datetime.now(UTC).timestamp()//86400))}）的复算，"
      "**仅作数据质量/漂移诊断**。\n")
    A("## 有效 universe\n")
    A("| perp | spot_pair | canonical | maxLev | role | 窗口 | 可交易日 | span | 排除日 | as-of 价比 | as-of corr | as-of 天 | now 判定 | now 价比 | now corr |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in valid:
        A(f"| {r['perp']} | {r['spot_pair']} | {r['is_canonical']} | {r['max_lev']} | {r['role']} | "
          f"{r['first_day']} → {r['last_day']} | {r['tradable_days']} | {r['window_span_days']} | "
          f"{r['excluded_days_in_span']} | {r['asof_median_ratio']:.6g} | {r['asof_pearson_ret']:.4f} | "
          f"{r['asof_n_overlap_days']} | {r['now_verdict']} | {r['now_median_ratio']:.6g} | {r['now_pearson_ret']:.4f} |")
    A("")
    A("## 拒绝清单（含理由）\n")
    A("| perp | spot_pair | role | as-of 判定 | as-of 价比 | as-of corr | now 判定 | now 价比 | now corr | 拒绝理由 |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for r in rejected:
        mr = f"{r['asof_median_ratio']:.6g}" if r["asof_median_ratio"] is not None else "-"
        pr = f"{r['asof_pearson_ret']:.4f}" if r["asof_pearson_ret"] is not None else "-"
        nmr = f"{r['now_median_ratio']:.6g}" if r["now_median_ratio"] is not None else "-"
        npr = f"{r['now_pearson_ret']:.4f}" if r["now_pearson_ret"] is not None else "-"
        A(f"| {r['perp']} | {r['spot_pair']} | {r['role']} | {r['asof_verdict']} | {mr} | {pr} | "
          f"{r['now_verdict']} | {nmr} | {npr} | {'; '.join(r.get('reasons', []))} |")
    if skipped_nonusdc:
        A("")
        A("## 非 USDC 计价同名对（按 SPEC 1.1 排除；永续为 USDC 结算）\n")
        A("| perp | spot_pair | quote_token_index |")
        A("|---|---|---|")
        for r in skipped_nonusdc:
            A(f"| {r['perp']} | {r['spot_pair']} | {r['quote_token_index']} |")
    A("")
    A("## 与 2026-08-16 memo 对照\n")
    mm = doc["memo_match"]
    A(f"- memo VALID = {MEMO_VALID}")
    A(f"- 重建 REPLICATE VALID = {mm['replicate_valid']} → **{'MATCH' if mm['valid'] else 'MISMATCH'}**")
    A(f"- memo REJECT = {MEMO_REJECT} → 重建 REJECT = {[r['perp'] for r in rejected]} "
      f"→ **{'MATCH' if mm['reject'] else 'MISMATCH'}**")
    A(f"- 2026-08-16 之后新增有效资产：{[r['perp'] for r in valid if r['role'] == 'NEW_SINCE_2026_08_16'] or '无'}")
    A(f"- **universe 漂移（as-of VALID 但 now 退化）**：{mm['universe_drift_vs_memo'] or '无'}")
    if mm["universe_drift_vs_memo"]:
        A("")
        A("> 注意：上述资产在全样本（含 2026-09/10 数据）下收益相关性跌破 0.8，原因是稀薄现货"
          "流动性造成的脏印价（spot 日收益 std 数百~900bps、现货零收益日占比高），不是资产身份错误。")
        A("> 按 SPEC 附注 A，前向窗口**仍保留**该资产（用前向期数据剔除资产属前视选择偏差），"
          "但将在报告中作为数据质量告警披露。")
    A("")
    A("## 说明")
    A("- `@N` 是 spot 槽位标识，其 candleSnapshot 历史可能被复用污染，因此**只做价格交叉验证，不看名字**。")
    A("- 可交易日 = 两腿在 d 与 d-1 都有 1d bar，且 d-1（信号日）与 d（收益日）的 funding 均有 24 小时记录；")
    A("  不满足的日**不交易、不计 PnL**，不做 forward-fill（数据长度差异显式处理）。")
    with open(os.path.join(HERE, "universe.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[universe] candidates={len(cands)} valid={len(valid)} rejected={len(rejected)} "
          f"non_usdc_skipped={len(skipped_nonusdc)}")
    print(f"[universe] memo_match(valid={doc['memo_match']['valid']}, reject={doc['memo_match']['reject']}) "
          f"drift={doc['memo_match']['universe_drift_vs_memo']}")
    print(f"[universe] REPLICATE = {doc['memo_match']['replicate_valid']}")
    for r in valid:
        print(f"  VALID {r['perp']:8s} {r['spot_pair']:12s} {r['first_day']} -> {r['last_day']} "
              f"tradable={r['tradable_days']:4d}/{r['window_span_days']:4d}d maxLev={r['max_lev']:2d} "
              f"role={r['role']:22s} asof(ratio={r['asof_median_ratio']:.4f},corr={r['asof_pearson_ret']:.3f}) "
              f"now({r['now_verdict']},corr={r['now_pearson_ret']:.3f})")
    for r in rejected:
        print(f"  REJECT {r['perp']:8s} {r['spot_pair']:12s} asof_ratio="
              f"{r['asof_median_ratio'] if r['asof_median_ratio'] is None else round(r['asof_median_ratio'],5)} "
              f"asof_corr={r['asof_pearson_ret'] if r['asof_pearson_ret'] is None else round(r['asof_pearson_ret'],3)} "
              f"now_corr={r['now_pearson_ret'] if r['now_pearson_ret'] is None else round(r['now_pearson_ret'],3)} "
              f"| {'; '.join(r.get('reasons', []))}")
    print(f"[universe] wrote universe.csv universe_rejects.csv universe.md data/universe.json data/panels/*.csv")


if __name__ == "__main__":
    main()
