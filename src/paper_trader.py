#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方向性交易 paper 账户（激进版）：分析 → 计划 → paper 成交 → 统计。

规则（激进但可控）：
- 单笔风险 2% 权益，最多 2 个并发仓位，名义杠杆上限 3x；
- taker 手续费 0.045%/边 + 滑点 0.05%/边（双边共 0.19%）；
- 每 15 分钟 tick：按 1h K 线高低点检查止损/止盈，先止损后止盈（保守）；
- 所有成交只记在本地 JSON，绝不发真实订单。

用法：
  python paper_trader.py init [--capital 1000]
  python paper_trader.py scan
  python paper_trader.py open --coin BTC --side long --stop 68000 --target 74000 --reason "..."
  python paper_trader.py close --coin BTC --reason "..."
  python paper_trader.py tick
  python paper_trader.py status
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
STATE = HERE / "account.json"
JOURNAL = HERE / "TRADES.md"
CACHE = HERE / "cache"
BASE_URL = "https://api.hyperliquid.xyz/info"
WORKDIR = HERE

RISK_PCT = 2.0
MAX_POSITIONS = 2
MAX_LEVERAGE = 3.0
FEE_TAKER = 0.00045
SLIPPAGE = 0.0005
WATCHLIST = ["BTC", "ETH", "HYPE", "SOL"]

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)


# ---------- 行情 ----------
def post(body: dict):
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(BASE_URL, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0 * (i + 1))
    raise RuntimeError(f"API 失败: {last}")


def candles(coin: str, interval: str = "1h", bars: int = 400) -> list[dict]:
    CACHE.mkdir(exist_ok=True)
    path = CACHE / f"{coin}_{interval}.json"
    if path.exists() and time.time() - path.stat().st_mtime < 600:
        return json.loads(path.read_text(encoding="utf-8"))[-bars:]
    rows = post({"type": "candleSnapshot",
                 "req": {"coin": coin, "interval": interval, "startTime": 0}})
    path.write_text(json.dumps(rows, ensure_ascii=True), encoding="utf-8")
    return rows[-bars:]


def ctxs() -> dict:
    try:
        data = post({"type": "metaAndAssetCtxs"})
        out = {}
        for m, c in zip(data[0]["universe"], data[1]):
            out[m["name"]] = c
        return out
    except Exception:  # noqa: BLE001
        return {}


def closes(rows, field="c"):
    return np.array([float(r[field]) for r in rows], dtype=float)


def ema(v: np.ndarray, n: int) -> np.ndarray:
    out = np.empty_like(v)
    alpha = 2 / (n + 1)
    out[0] = v[0]
    for i in range(1, len(v)):
        out[i] = alpha * v[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(v: np.ndarray, n: int = 14) -> float:
    if len(v) < n + 1:
        return 50.0
    d = np.diff(v)
    up = np.clip(d, 0, None)
    dn = np.clip(-d, 0, None)
    au, ad = up[:n].mean(), dn[:n].mean()
    for i in range(n, len(d)):
        au = (au * (n - 1) + up[i]) / n
        ad = (ad * (n - 1) + dn[i]) / n
    return 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)


def atr(rows, n: int = 14) -> float:
    if len(rows) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = float(rows[i]["h"]), float(rows[i]["l"]), float(rows[i - 1]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return float(np.mean(trs[-n:]))


def snapshot(coin: str) -> dict:
    rows4 = candles(coin, "4h", 400)
    rows1 = candles(coin, "1h", 240)
    c4 = closes(rows4)
    c1 = closes(rows1)
    e20, e50, e200 = ema(c4, 20), ema(c4, 50), ema(c4, 200)
    a = atr(rows4)
    ctx = ctxs().get(coin, {})
    return {
        "coin": coin,
        "last": round(float(c4[-1]), 6),
        "chg24h_pct": round((c1[-1] / c1[-25] - 1) * 100, 2) if len(c1) > 25 else 0.0,
        "ema20_4h": round(float(e20[-1]), 6),
        "ema50_4h": round(float(e50[-1]), 6),
        "ema200_4h": round(float(e200[-1]), 6),
        "trend": "bull" if e20[-1] > e50[-1] > e200[-1] else ("bear" if e20[-1] < e50[-1] < e200[-1] else "mixed"),
        "rsi14_4h": round(rsi(c4), 1),
        "atr14_4h": round(a, 6),
        "atr_pct": round(a / float(c4[-1]) * 100, 2),
        "funding": float(ctx.get("funding", 0) or 0),
        "oi": ctx.get("openInterest"),
        "day_vol": ctx.get("dayNtlVlm"),
    }


def cached_price(coin: str) -> float | None:
    path = CACHE / f"{coin}_1h.json"
    if path.exists():
        try:
            return float(json.loads(path.read_text(encoding="utf-8"))[-1]["c"])
        except Exception:  # noqa: BLE001
            return None
    return None


def mark_price(coin: str) -> float:
    rows = candles(coin, "1h", 5)
    return float(rows[-1]["c"])


# ---------- 账户 ----------
def load() -> dict:
    if not STATE.exists():
        return {}
    return json.loads(STATE.read_text(encoding="utf-8"))


def save(a: dict) -> None:
    STATE.write_text(json.dumps(a, ensure_ascii=False, indent=2), encoding="utf-8")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def equity(a: dict) -> float:
    return a["capital"] + a["realized_pnl"]


def init_account(capital: float) -> dict:
    a = {
        "capital": capital,
        "realized_pnl": 0.0,
        "fees_paid": 0.0,
        "positions": {},
        "closed": [],
        "equity_curve": [{"ts": now_iso(), "equity": capital}],
        "created_at": now_iso(),
        "config": {"risk_pct": RISK_PCT, "max_positions": MAX_POSITIONS,
                   "max_leverage": MAX_LEVERAGE, "fee": FEE_TAKER, "slippage": SLIPPAGE},
    }
    save(a)
    return a


def cost_rate() -> float:
    return FEE_TAKER + SLIPPAGE


def open_position(a: dict, coin: str, side: str, stop: float, target: float, reason: str) -> dict:
    if coin in a["positions"]:
        return {"ok": False, "error": f"{coin} 已有持仓"}
    if len(a["positions"]) >= MAX_POSITIONS:
        return {"ok": False, "error": f"已达最大持仓数 {MAX_POSITIONS}"}
    side = side.lower()
    if side not in ("long", "short"):
        return {"ok": False, "error": "side 必须是 long/short"}
    entry = mark_price(coin)
    if side == "long" and not (stop < entry < target):
        return {"ok": False, "error": f"多头需要 stop<entry<target（entry={entry}）"}
    if side == "short" and not (target < entry < stop):
        return {"ok": False, "error": f"空头需要 target<entry<stop（entry={entry}）"}
    eq = equity(a)
    stop_dist = abs(entry - stop) / entry
    if stop_dist <= 0.002:
        return {"ok": False, "error": "止损距离过小（<0.2%）"}
    risk_usd = eq * RISK_PCT / 100
    notional = risk_usd / stop_dist
    max_notional = eq * MAX_LEVERAGE
    notional = min(notional, max_notional)
    qty = notional / entry
    fee = notional * cost_rate()
    a["realized_pnl"] -= fee
    a["fees_paid"] += fee
    a["positions"][coin] = {
        "coin": coin, "side": side, "entry": round(entry, 6), "qty": qty,
        "notional": round(notional, 2), "stop": stop, "target": target,
        "risk_usd": round(risk_usd, 2), "opened_at": now_iso(), "reason": reason,
        "entry_fee": round(fee, 4),
    }
    save(a)
    return {"ok": True, "position": a["positions"][coin], "fee": round(fee, 4),
            "equity": round(equity(a), 2)}


def close_position(a: dict, coin: str, reason: str, exit_price: float | None = None) -> dict:
    pos = a["positions"].get(coin)
    if not pos:
        return {"ok": False, "error": f"{coin} 无持仓"}
    px = exit_price if exit_price is not None else mark_price(coin)
    direction = 1 if pos["side"] == "long" else -1
    gross = (px - pos["entry"]) * pos["qty"] * direction
    exit_fee = pos["notional"] * cost_rate()
    net = gross - exit_fee
    a["realized_pnl"] += net
    a["fees_paid"] += exit_fee
    r_mult = net / pos["risk_usd"] if pos["risk_usd"] else 0.0
    trade = {**pos, "exit": round(px, 6), "closed_at": now_iso(), "exit_fee": round(exit_fee, 4),
             "gross": round(gross, 4), "net": round(net, 4), "r_multiple": round(r_mult, 3),
             "exit_reason": reason}
    a["closed"].append(trade)
    del a["positions"][coin]
    a["equity_curve"].append({"ts": now_iso(), "equity": round(equity(a), 2)})
    save(a)
    return {"ok": True, "trade": trade, "equity": round(equity(a), 2)}


def tick(a: dict) -> list[dict]:
    events = []
    for coin, pos in list(a["positions"].items()):
        rows = candles(coin, "1h", 10)
        last = rows[-1]
        hi, lo = float(last["h"]), float(last["l"])
        if pos["side"] == "long":
            if lo <= pos["stop"]:
                events.append({"coin": coin, "action": "stopped",
                               **close_position(a, coin, "止损（1h 低点触及）", pos["stop"])})
            elif hi >= pos["target"]:
                events.append({"coin": coin, "action": "target",
                               **close_position(a, coin, "止盈（1h 高点触及）", pos["target"])})
        else:
            if hi >= pos["stop"]:
                events.append({"coin": coin, "action": "stopped",
                               **close_position(a, coin, "止损（1h 高点触及）", pos["stop"])})
            elif lo <= pos["target"]:
                events.append({"coin": coin, "action": "target",
                               **close_position(a, coin, "止盈（1h 低点触及）", pos["target"])})
    # 记录权益曲线
    if a:
        a = load()
        a["equity_curve"].append({"ts": now_iso(), "equity": round(equity(a), 2)})
        a["equity_curve"] = a["equity_curve"][-2000:]
        save(a)
    return events


def stats(a: dict, offline: bool = False) -> dict:
    closed = a.get("closed", [])
    eq = equity(a)
    unreal = 0.0
    for coin, pos in a["positions"].items():
        px = None
        if offline:
            px = cached_price(coin)
        if px is None:
            try:
                px = mark_price(coin)
            except Exception:  # noqa: BLE001  行情接口异常时用入场价兜底，避免统计失败
                px = pos["entry"]
        d = 1 if pos["side"] == "long" else -1
        unreal += (px - pos["entry"]) * pos["qty"] * d
    curve = [p["equity"] for p in a.get("equity_curve", [])] or [eq]
    peak = np.maximum.accumulate(curve)
    mdd = float(((peak - np.array(curve)) / peak).max() * 100) if peak.max() > 0 else 0.0
    wins = [t for t in closed if t["net"] > 0]
    losses = [t for t in closed if t["net"] <= 0]
    rs = [t["r_multiple"] for t in closed]
    return {
        "capital": a.get("capital"), "equity": round(eq, 2), "realized_pnl": round(a["realized_pnl"], 4),
        "unrealized_pnl": round(unreal, 4), "fees_paid": round(a["fees_paid"], 4),
        "trades": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_r": round(float(np.mean(rs)), 3) if rs else None,
        "expectancy_r": round(float(np.mean(rs)), 3) if rs else None,
        "profit_factor": round(sum(t["net"] for t in wins) / abs(sum(t["net"] for t in losses)), 2)
        if losses and sum(t["net"] for t in losses) != 0 else None,
        "max_dd_pct": round(mdd, 2),
        "positions": len(a["positions"]),
    }


def render_status(a: dict) -> str:
    s = stats(a)
    lines = [
        "# 方向性交易 Paper 账户（激进版）", "",
        f"- 更新时间：{now_iso()}",
        f"- 初始资本：${s['capital']:,.2f} ｜ 当前权益：**${s['equity']:,.2f}**（已实现 {s['realized_pnl']:+.2f} / 未实现 {s['unrealized_pnl']:+.2f}）",
        f"- 交易：{s['trades']} 笔 ｜ 胜率 {s['win_rate_pct']}% ｜ 平均 R {s['avg_r']} ｜ 盈亏比 {s['profit_factor']} ｜ 最大回撤 {s['max_dd_pct']}%",
        f"- 持仓：{s['positions']} 个 ｜ 手续费：${s['fees_paid']:.2f}",
        "",
    ]
    if a["positions"]:
        lines += ["## 当前持仓", "", "| 币 | 方向 | 入场 | 名义 | 止损 | 止盈 | 风险$ |", "|---|---|---:|---:|---:|---:|---:|"]
        for c, p in a["positions"].items():
            lines.append(f"| {c} | {p['side']} | {p['entry']} | ${p['notional']:,.2f} | {p['stop']} | {p['target']} | {p['risk_usd']} |")
        lines.append("")
    if a["closed"]:
        lines += ["## 最近平仓（最近 10 笔）", "", "| 平仓时间 | 币 | 方向 | 入场 | 出场 | 净盈亏$ | R | 原因 |", "|---|---|---|---:|---:|---:|---:|---|"]
        for t in a["closed"][-10:]:
            lines.append(f"| {t['closed_at']} | {t['coin']} | {t['side']} | {t['entry']} | {t['exit']} | "
                         f"{t['net']:+.2f} | {t['r_multiple']:+.2f} | {t['exit_reason']} |")
        lines.append("")
    lines += ["> paper 模拟盘：不产生真实成交、不涉及真实资金。", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init")
    p_init.add_argument("--capital", type=float, default=1000.0)
    sub.add_parser("scan")
    sub.add_parser("status")
    sub.add_parser("tick")
    p_open = sub.add_parser("open")
    p_open.add_argument("--coin", required=True)
    p_open.add_argument("--side", required=True)
    p_open.add_argument("--stop", type=float, required=True)
    p_open.add_argument("--target", type=float, required=True)
    p_open.add_argument("--reason", required=True)
    p_close = sub.add_parser("close")
    p_close.add_argument("--coin", required=True)
    p_close.add_argument("--reason", default="手动平仓")
    args = ap.parse_args()

    if args.cmd == "init":
        a = init_account(args.capital)
        print(json.dumps(a, ensure_ascii=False, indent=2))
        return 0
    a = load()
    if not a:
        print("账户未初始化：先运行 python paper_trader.py init")
        return 1

    if args.cmd == "scan":
        out = [snapshot(c) for c in WATCHLIST]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "open":
        r = open_position(a, args.coin.upper(), args.side, args.stop, args.target, args.reason)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        JOURNAL.parent.mkdir(exist_ok=True)
        if r.get("ok"):
            with JOURNAL.open("a", encoding="utf-8") as f:
                pos = r["position"]
                f.write(f"\n## {pos['opened_at']} 开仓 {pos['coin']} {pos['side']}\n\n")
                f.write(f"- 入场：{pos['entry']}｜名义：${pos['notional']:,.2f}｜止损：{pos['stop']}｜止盈：{pos['target']}\n")
                f.write(f"- 风险：${pos['risk_usd']}｜理由：{pos['reason']}\n")
        return 0 if r.get("ok") else 1
    if args.cmd == "close":
        r = close_position(a, args.coin.upper(), args.reason)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if r.get("ok"):
            t = r["trade"]
            with JOURNAL.open("a", encoding="utf-8") as f:
                f.write(f"\n## {t['closed_at']} 平仓 {t['coin']} {t['side']}\n\n")
                f.write(f"- 入场 {t['entry']} → 出场 {t['exit']}｜净盈亏 ${t['net']:+.2f}（{t['r_multiple']:+.2f}R）\n")
                f.write(f"- 原因：{t['exit_reason']}\n")
        return 0 if r.get("ok") else 1
    if args.cmd == "tick":
        events = tick(a)
        print(json.dumps(events, ensure_ascii=False, indent=2) if events else "无触发")
        return 0
    if args.cmd == "status":
        a = load()
        print(render_status(a))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())



