#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选资产 carry 快速筛选：BTC/ETH/SOL 等现货-永续配对的资金费 carry 历史表现。

注意：这是"快速筛选"，不是完整验证（未做 1h 级、未做容量/清算细节、未做 paper）。
输出：CANDIDATE_CARRY_SCREEN.md / candidate_carry.json
数据缓存：data_candidates/（原始 JSON，已 gitignore）
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "data_candidates"
OUT_JSON = HERE / "candidate_carry.json"
OUT_MD = HERE / "CANDIDATE_CARRY_SCREEN.md"
BASE_URL = "https://api.hyperliquid.xyz/info"
CAPITAL = 1000.0
COST_RATE = 0.00045 + 0.00070 + 2 * 0.0015  # 0.415%/边
CANDIDATES = ["BTC", "ETH", "SOL", "ZEC", "ENA", "XPL", "AVAX"]

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)


def post(body: dict) -> object:
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(BASE_URL, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"API 失败: {last}")


def fetch_cached(name: str, body: dict) -> object:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{name}.json"
    if path.exists() and path.stat().st_size > 100:
        return json.loads(path.read_text(encoding="utf-8"))
    data = post(body)
    path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    return data


def fetch_funding_all(coin: str) -> list[dict]:
    """分页拉取全部 fundingHistory（API 每次最多 500 条）。"""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"funding_all_{coin}.json"
    if path.exists() and path.stat().st_size > 1000:
        return json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict] = []
    start = 0
    for _ in range(200):
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
    path.write_text(json.dumps(rows, ensure_ascii=True), encoding="utf-8")
    return rows


def main() -> int:
    meta, ctxs = post({"type": "metaAndAssetCtxs"})
    idx = {u["name"]: i for i, u in enumerate(meta["universe"])}
    spot_meta, spot_ctxs = post({"type": "spotMetaAndAssetCtxs"})
    tokens = {t["index"]: t for t in spot_meta["tokens"]}
    pairs = {p["name"]: p for p in spot_meta["universe"]}
    spot_ctx: dict[str, dict] = {}
    for c in spot_ctxs:
        prev = spot_ctx.get(c["coin"])
        if prev is None or float(c.get("dayNtlVlm") or 0) > float(prev.get("dayNtlVlm") or 0):
            spot_ctx[c["coin"]] = c

    coin_to_pair: dict[str, str] = {}
    for pair_name, pair in pairs.items():
        tok = tokens.get(pair["tokens"][0])
        if not tok:
            continue
        tn = tok["name"]
        coin = tn if tn in idx else (tn[1:] if tn.startswith(("U", "u")) and tn[1:] in idx else None)
        if coin and pair_name in spot_ctx:
            prev = coin_to_pair.get(coin)
            if prev is None or float(spot_ctx[pair_name].get("dayNtlVlm") or 0) > \
                    float(spot_ctx[prev].get("dayNtlVlm") or 0):
                coin_to_pair[coin] = pair_name

    rows = []
    for coin in CANDIDATES:
        if coin not in idx or coin not in coin_to_pair:
            rows.append({"asset": coin, "status": "NO_SPOT_PERP_PAIR"})
            continue
        try:
            pair = coin_to_pair[coin]
            max_lev = int(meta["universe"][idx[coin]].get("maxLeverage", 1))
            spot_c = fetch_cached(f"spot_1d_{coin}", {"type": "candleSnapshot",
                                                      "req": {"coin": pair, "interval": "1d", "startTime": 0}})
            perp_c = fetch_cached(f"perp_1d_{coin}", {"type": "candleSnapshot",
                                                      "req": {"coin": coin, "interval": "1d", "startTime": 0}})
            funding = fetch_funding_all(coin)
            spot_close = {datetime.fromtimestamp(int(b["t"]) / 1000, tz=timezone.utc).date(): float(b["c"])
                          for b in spot_c}
            perp_close = {datetime.fromtimestamp(int(b["t"]) / 1000, tz=timezone.utc).date(): float(b["c"])
                          for b in perp_c}
            fund_day: dict = {}
            for r in funding:
                d = datetime.fromtimestamp(int(r["time"]) / 1000, tz=timezone.utc).date()
                fund_day[d] = fund_day.get(d, 0.0) + float(r["fundingRate"])
            days = sorted(set(spot_close) & set(perp_close) & set(fund_day))
            days = [d for d in days if 0.5 <= (spot_close[d] / perp_close[d] if perp_close[d] else 0) <= 2.0]
            if len(days) < 30:
                rows.append({"asset": coin, "status": f"INSUFFICIENT_DATA days={len(days)}"})
                continue
            N = CAPITAL / (1 + 1 / max_lev)
            funding_usd = basis_usd = 0.0
            prev_s = prev_p = None
            anomalies = 0
            for d in days:
                funding_usd += fund_day[d] * N
                if prev_s is not None:
                    sr = (spot_close[d] - prev_s) / prev_s
                    pr = (perp_close[d] - prev_p) / prev_p
                    # 数据质量：单腿异常跳动（|sr-pr|>10%）视为脏印价，跳过当日 basis
                    if abs(sr - pr) <= 0.10:
                        basis_usd += sr * N - pr * N
                    else:
                        anomalies += 1
                prev_s, prev_p = spot_close[d], perp_close[d]
            pnl = funding_usd + basis_usd - COST_RATE * N
            span_days = max((days[-1] - days[0]).days, 1)
            ret = pnl / CAPITAL
            cagr = (1 + ret) ** (365 / span_days) - 1
            rows.append({
                "asset": coin, "status": "OK", "pair": pair, "max_lev": max_lev,
                "days": len(days), "period": f"{days[0]} → {days[-1]}",
                "funding_usd": round(funding_usd, 2), "basis_usd": round(basis_usd, 2),
                "total_return_pct": round(ret * 100, 2), "cagr_pct": round(cagr * 100, 2),
                "funding_yield_annual_pct": round(funding_usd / CAPITAL / span_days * 365 * 100, 2),
                "notional_usd": round(N, 2), "anomaly_days": anomalies,
            })
        except Exception as exc:  # noqa: BLE001
            rows.append({"asset": coin, "status": f"ERROR: {exc}"})

    result = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "capital": CAPITAL, "cost_rate": COST_RATE, "results": rows}
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# 候选资产 carry 快速筛选", "",
        f"- 生成时间：{result['generated_at']}",
        "- 口径：$1,000 全仓单一资产（1x delta-neutral）、taker+15bps 成本、按日线回放；**筛选级，不是完整验证**", "",
        "| 资产 | 数据天数 | 区间 | 资金费收益/年 | 总收益 | 年化 | basis 贡献 |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for r in rows:
        if r.get("status") != "OK":
            md.append(f"| {r['asset']} | — | — | — | — | — | 状态：{r.get('status')} |")
            continue
        md.append(f"| {r['asset']} | {r['days']} | {r['period']} | {r['funding_yield_annual_pct']:+.1f}% | "
                  f"{r['total_return_pct']:+.2f}% | {r['cagr_pct']:+.2f}% | ${r['basis_usd']:+,.2f} |")
    md += [
        "",
        "## 解读",
        "",
        "- 资金费收益/年 = 纯资金费（不含 basis/成本）年化；",
        "- 年化 = 扣成本后（含 basis）的 A0 年化；",
        "- **这是筛选级结论**：日线近似、未做 1h 级验证、未做容量/清算/paper；"
        "若某资产资金费年化明显低于 10%，可直接排除；若接近或高于 10%，才值得进入完整验证流程。",
        "",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



