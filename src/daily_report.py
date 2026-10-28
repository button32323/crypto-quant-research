#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""方向性交易 paper 日报：当日盈亏、持仓、累计统计、市场快照。

用法：python daily_report.py
输出：DAILY_REPORT.md（最新） + reports/DAILY_REPORT_<日期>.md（历史）
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORTS = HERE / "reports"
OUT = HERE / "DAILY_REPORT.md"
STATE = HERE / "account.json"

import sys
sys.path.insert(0, str(HERE))
import paper_trader as pt  # noqa: E402


def main() -> int:
    a = pt.load()
    if not a:
        print("账户未初始化")
        return 1
    s = pt.stats(a, offline=True)
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%H:%M")

    # 当日平仓
    closed_today = [t for t in a.get("closed", []) if t.get("closed_at", "").startswith(today)]
    day_pnl = sum(t["net"] for t in closed_today)
    # 当日权益变化（取本地日期最早的 curve 点）
    curve = a.get("equity_curve", [])
    day_start_eq = s["equity"]
    for p in curve:
        try:
            dt = datetime.fromisoformat(p["ts"])
        except Exception:  # noqa: BLE001
            continue
        if dt.astimezone().strftime("%Y-%m-%d") == today:
            day_start_eq = float(p["equity"])
            break
    day_change = s["equity"] - day_start_eq

    # 持仓详情
    rows = []
    for coin, p in a["positions"].items():
        px = pt.cached_price(coin)
        if px is None:
            try:
                px = pt.mark_price(coin)
            except Exception:  # noqa: BLE001  行情异常时用入场价兜底，日报照常生成
                px = p["entry"]
        d = 1 if p["side"] == "long" else -1
        unreal = (px - p["entry"]) * p["qty"] * d
        dist_stop = (p["stop"] / px - 1) * 100 if p["side"] == "long" else (px / p["stop"] - 1) * 100
        rows.append((coin, p["side"], p["entry"], px, p["notional"], p["stop"], p["target"],
                     p["risk_usd"], unreal, dist_stop))

    # 市场快照（离线：读本地缓存，避免接口慢拖住日报）
    from datetime import datetime as _dt, timezone as _tz
    snaps = []
    for c in ("BTC", "ETH", "HYPE", "SOL"):
        try:
            candles = json.loads((pt.CACHE / f"{c}_4h.json").read_text(encoding="utf-8"))
            cl = pt.closes(candles)
            if len(cl) < 30:
                continue
            e20, e50, e200 = pt.ema(cl, 20), pt.ema(cl, 50), pt.ema(cl, 200)
            trend = "bull" if e20[-1] > e50[-1] > e200[-1] else ("bear" if e20[-1] < e50[-1] < e200[-1] else "mixed")
            ts = _dt.fromtimestamp(int(candles[-1]["t"]) / 1000, tz=_tz.utc).strftime("%m-%d %H:%M")
            snaps.append({
                "coin": c, "last": round(float(cl[-1]), 4),
                "chg24h_pct": round((cl[-1] / cl[-25] - 1) * 100, 2) if len(cl) > 25 else 0.0,
                "trend": trend, "rsi14_4h": round(pt.rsi(cl), 1),
                "atr_pct": round(pt.atr(candles) / float(cl[-1]) * 100, 2), "cache_ts": ts,
            })
        except Exception:  # noqa: BLE001
            pass

    lines = [
        "# 方向性交易 Paper 日报 — " + today,
        "",
        f"- 生成时间：{today} {now}（本地）",
        f"- 账户权益：**${s['equity']:,.2f}**（初始 ${s['capital']:,.2f}）",
        f"- 当日已平仓盈亏：**${day_pnl:+,.2f}** ｜ 当日权益变化：**${day_change:+,.2f}**",
        f"- 累计：{s['trades']} 笔 ｜ 胜率 {s['win_rate_pct']}% ｜ 平均 R {s['avg_r']} ｜ 盈亏比 {s['profit_factor']} ｜ 最大回撤 {s['max_dd_pct']}%",
        f"- 手续费累计：${s['fees_paid']:.2f} ｜ 未实现盈亏：${s['unrealized_pnl']:+,.2f}",
        "",
        "## 当前持仓",
        "",
    ]
    if rows:
        lines += ["| 币 | 方向 | 入场 | 现价 | 名义 | 止损 | 止盈 | 风险$ | 未实现$ | 距止损% |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for r in rows:
            lines.append(f"| {r[0]} | {r[1]} | {r[2]} | {r[3]:.4f} | ${r[4]:,.2f} | {r[5]} | {r[6]} | "
                         f"${r[7]:.2f} | {r[8]:+.2f} | {r[9]:.2f}% |")
    else:
        lines.append("（无持仓）")
    lines += ["", "## 今日平仓", ""]
    if closed_today:
        lines += ["| 时间 | 币 | 方向 | 入场 | 出场 | 净盈亏$ | R | 原因 |",
                  "|---|---|---|---:|---:|---:|---:|---|"]
        for t in closed_today:
            lines.append(f"| {t['closed_at']} | {t['coin']} | {t['side']} | {t['entry']} | {t['exit']} | "
                         f"{t['net']:+.2f} | {t['r_multiple']:+.2f} | {t['exit_reason']} |")
    else:
        lines.append("（今日无平仓）")
    lines += ["", "## 市场快照（4h，本地缓存）", "",
              "| 币 | 最新价 | 24h% | 趋势 | RSI | ATR% | 缓存时间(UTC) |",
              "|---|---:|---:|---|---:|---:|---|"]
    for sn in snaps:
        lines.append(f"| {sn['coin']} | {sn['last']} | {sn['chg24h_pct']:+.2f}% | {sn['trend']} | "
                     f"{sn['rsi14_4h']} | {sn['atr_pct']}% | {sn.get('cache_ts','-')} |")
    lines += [
        "",
        "## 说明",
        "",
        "- paper 模拟盘，不产生真实成交；",
        "- 风控：单笔风险 2%、最多 2 仓、3x 上限；15 分钟监控自动止损/止盈；",
        "- 累计统计在 `account.json`，交易流水在 `TRADES.md`。",
        "",
    ]
    text = "\n".join(lines)
    OUT.write_text(text, encoding="utf-8")
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"DAILY_REPORT_{today}.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"\n已生成: {OUT} 与 {REPORTS / ('DAILY_REPORT_' + today + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



