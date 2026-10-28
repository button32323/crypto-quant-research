#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓对账：把交易所账户快照与预期 delta-neutral 状态比较（只读校验）。

用法：
    python reconcile.py --actual snapshot.json
    python reconcile.py --actual snapshot.json --expected state.json --capital 1000
输出：
    reconcile.json / RECONCILIATION.md

snapshot.json 格式（由用户从交易所导出或手工填写）：
{
  "captured_at": "2026-10-24T08:00:00Z",
  "capital_usd": 1000,
  "spot": {"PURR": {"qty": 3966, "price": 0.0945},
           "HYPE": {"qty": 5.97, "price": 76.1}},
  "perp": {"PURR": {"qty": -3955, "price": 0.0948, "margin": 125},
           "HYPE": {"qty": -5.97, "price": 76.07, "margin": 45.45}}
}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_JSON = HERE / "reconcile.json"
OUT_MD = HERE / "RECONCILIATION.md"

MAX_DELTA_PCT = 5.0      # 净 delta 超过资本 5% 报警
MAX_LEVERAGE = 1.0       # 总名义/资本上限
QTY_TOLERANCE = 0.01     # 与预期数量偏差 1%


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="持仓对账（只读）")
    ap.add_argument("--actual", required=True, help="账户快照 JSON")
    ap.add_argument("--expected", default=None, help="预期持仓 JSON（默认 paper state.json）")
    ap.add_argument("--capital", type=float, default=None, help="覆盖资本（USD）")
    args = ap.parse_args()

    actual = load(Path(args.actual))
    capital = args.capital or actual.get("capital_usd") or 1000.0
    expected = load(Path(args.expected)) if args.expected else (
        load(HERE / "state.json") if (HERE / "state.json").exists() else None)

    assets = sorted(set(actual.get("spot", {})) | set(actual.get("perp", {})))
    checks: list[dict] = []
    total_spot = 0.0
    total_perp = 0.0
    net_delta = 0.0
    for a in assets:
        spot = actual.get("spot", {}).get(a)
        perp = actual.get("perp", {}).get(a)
        if spot and perp:
            s_notional = float(spot["qty"]) * float(spot["price"])
            p_notional = abs(float(perp["qty"])) * float(perp["price"])
            total_spot += s_notional
            total_perp += p_notional
            net_delta += s_notional - p_notional
            checks.append({"check": f"{a} 两腿存在", "ok": True,
                           "detail": f"spot ${s_notional:,.2f} / perp ${p_notional:,.2f}"})
        elif spot:
            s_notional = float(spot["qty"]) * float(spot["price"])
            total_spot += s_notional
            net_delta += s_notional
            checks.append({"check": f"{a} 两腿存在", "ok": False,
                           "detail": f"缺永续对冲腿（裸多 ${s_notional:,.2f}）"})
        else:
            p_notional = abs(float(perp["qty"])) * float(perp["price"])
            total_perp += p_notional
            net_delta -= p_notional
            checks.append({"check": f"{a} 两腿存在", "ok": False,
                           "detail": f"缺现货腿（裸空 ${p_notional:,.2f}）"})

        if expected and a in expected.get("positions", {}):
            exp = expected["positions"][a]
            if spot and exp.get("qty_spot"):
                exp_q = float(exp["qty_spot"])
                dev = abs(float(spot["qty"]) - exp_q) / exp_q if exp_q else 1.0
                checks.append({"check": f"{a} 现货数量与预期一致", "ok": dev <= QTY_TOLERANCE,
                               "detail": f"实际 {spot['qty']:.4f} vs 预期 {exp_q:.4f}（偏差 {dev:.2%}）"})
            if perp and exp.get("qty_perp"):
                exp_q = float(exp["qty_perp"])
                dev = abs(abs(float(perp["qty"])) - exp_q) / exp_q if exp_q else 1.0
                checks.append({"check": f"{a} 永续数量与预期一致", "ok": dev <= QTY_TOLERANCE,
                               "detail": f"实际 {abs(float(perp['qty'])):.4f} vs 预期 {exp_q:.4f}（偏差 {dev:.2%}）"})

    delta_pct = abs(net_delta) / capital * 100 if capital else 0.0
    # 口径说明：delta-neutral 天然是"两侧各 1x"（gross ≈2x）。
    # 风控上限按"单侧名义/资本"计（与 risk_rules.check_portfolio 一致），gross 仅作展示。
    one_side_notional = (total_spot + total_perp) / 2
    leverage = one_side_notional / capital if capital else 0.0
    gross_leverage = (total_spot + total_perp) / capital if capital else 0.0
    checks.append({"check": "净 delta 中性", "ok": delta_pct <= MAX_DELTA_PCT,
                   "detail": f"|spot-perp| = ${abs(net_delta):,.2f}（资本的 {delta_pct:.2f}%，阈值 {MAX_DELTA_PCT:.0f}%）"})
    checks.append({"check": "单侧杠杆 ≤1x", "ok": leverage <= MAX_LEVERAGE + 1e-9,
                   "detail": f"单侧名义 ${one_side_notional:,.2f} / 资本 ${capital:,.2f} = {leverage:.2f}x"
                             f"（gross {gross_leverage:.2f}x 为 delta-neutral 正常现象）"})

    overall = "PASS" if all(c["ok"] for c in checks) else "FAIL"
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "captured_at": actual.get("captured_at"),
        "capital_usd": capital,
        "total_spot_usd": round(total_spot, 2),
        "total_perp_usd": round(total_perp, 2),
        "net_delta_usd": round(net_delta, 2),
        "delta_pct_of_capital": round(delta_pct, 2),
        "leverage": round(leverage, 2),
        "gross_leverage": round(gross_leverage, 2),
        "overall": overall,
        "checks": checks,
    }
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# 持仓对账（只读）",
        "",
        f"- 生成时间：{result['generated_at']} ｜ 快照时间：{result['captured_at']}",
        f"- 资本：${capital:,.2f} ｜ 整体：**{overall}**",
        "",
        "| 检查 | 结果 | 详情 |",
        "|---|---|---|",
    ]
    for c in checks:
        md.append(f"| {c['check']} | {'✅' if c['ok'] else '❌'} | {c['detail']} |")
    md += [
        "",
        "## 汇总",
        "",
        f"- 现货名义：${result['total_spot_usd']:,.2f}",
        f"- 永续名义：${result['total_perp_usd']:,.2f}",
        f"- 净 delta：${result['net_delta_usd']:+,.2f}（资本的 {result['delta_pct_of_capital']:.2f}%）",
        f"- 单侧杠杆：{result['leverage']:.2f}x（gross {result['gross_leverage']:.2f}x，delta-neutral 正常）",
        "",
        "## 建议",
        "",
        "- FAIL：先不要开新仓/加仓，按上表逐项核查；裸腿必须立即补齐。",
        "- PASS：继续按日检查资金费、保证金率与回撤。",
        "",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({"overall": overall, "delta_pct": result["delta_pct_of_capital"],
                      "leverage": result["leverage"],
                      "failed": [c["check"] for c in checks if not c["ok"]]},
                     ensure_ascii=False, indent=2))
    print(f"已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
