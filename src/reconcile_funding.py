#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金费对账：独立从 Hyperliquid 拉取 fundingHistory，与 paper state 的累计值对比。

这是预注册 30 天验证条件之一（"数据对账干净"）的实现：
即使 paper 自身是用同一 API 累计的，独立重算一次也能发现时区/时间戳/重复结算/漏结算问题。

用法：
    python reconcile_funding.py                 # 默认 state.json
    python reconcile_funding.py --state state.json --tolerance 0.01
退出码：0 = PASS，1 = FAIL
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import paper_hl_carry as ph  # noqa: E402  (复用 fetch_funding_since)


def now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def created_ms(state: dict) -> int:
    dt = datetime.fromisoformat(state["created_at"])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return int(dt.timestamp() * 1000)


def reconcile(state: dict, fetch_fn, tolerance: float = 0.01) -> dict:
    start = created_ms(state)
    end = now_ms()
    checks: list[dict] = []
    expected_total = 0.0
    for name, pos in state["positions"].items():
        perp = pos["perp"]
        notional = float(pos["notional_usd"])
        rows = fetch_fn(perp, start)
        expected = sum(float(r["fundingRate"]) * notional for r in rows)
        expected_total += expected
        stats = state.get("funding_stats", {}).get(name, {"settlements": 0, "positive": 0})
        n = len(rows)
        pos_n = sum(1 for r in rows if float(r["fundingRate"]) > 0)
        checks.append({"check": f"{name} 结算次数一致", "ok": n == stats.get("settlements", -1),
                       "detail": f"期望 {n} vs 记录 {stats.get('settlements')}"})
        checks.append({"check": f"{name} 正费率次数一致", "ok": pos_n == stats.get("positive", -1),
                       "detail": f"期望 {pos_n} vs 记录 {stats.get('positive')}"})
        if rows:
            last_ts = int(rows[-1]["time"])
            recorded = int(state.get("last_funding_ts_ms", {}).get(perp, 0))
            checks.append({"check": f"{name} 时间戳已消费到底", "ok": recorded >= last_ts,
                           "detail": f"记录 {recorded} vs 最新 {last_ts}"})
        else:
            checks.append({"check": f"{name} 无待处理结算", "ok": True, "detail": "窗口内无新结算"})

    paper_total = float(state.get("funding_accrued_usd", 0.0))
    diff = expected_total - paper_total
    tol = max(tolerance, abs(expected_total) * 0.01)
    checks.append({"check": "累计资金费金额对账", "ok": abs(diff) <= tol,
                   "detail": f"独立重算 ${expected_total:+.6f} vs paper 记录 ${paper_total:+.6f}"
                             f"（差 ${diff:+.6f}，容差 ${tol:.4f}）"})
    overall = "PASS" if all(c["ok"] for c in checks) else "FAIL"
    return {
        "period_start": datetime.fromtimestamp(start / 1000).astimezone().isoformat(timespec="seconds"),
        "period_end": datetime.fromtimestamp(end / 1000).astimezone().isoformat(timespec="seconds"),
        "expected_funding_usd": round(expected_total, 6),
        "paper_funding_usd": round(paper_total, 6),
        "diff_usd": round(diff, 6),
        "tolerance_usd": round(tol, 4),
        "overall": overall,
        "checks": checks,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="资金费对账（独立重算）")
    ap.add_argument("--state", default=str(HERE / "state.json"))
    ap.add_argument("--tolerance", type=float, default=0.01)
    args = ap.parse_args()
    state = json.loads(Path(args.state).read_text(encoding="utf-8"))
    result = reconcile(state, ph.fetch_funding_since, args.tolerance)
    out = HERE / "funding_reconcile.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("overall", "expected_funding_usd",
                                             "paper_funding_usd", "diff_usd")},
                     ensure_ascii=False, indent=2))
    for c in result["checks"]:
        if not c["ok"]:
            print(f"FAIL {c['check']}: {c['detail']}")
    print(f"已生成: {out}")
    return 0 if result["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
