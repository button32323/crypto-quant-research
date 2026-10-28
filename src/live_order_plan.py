#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘下单计划生成器（DRY RUN：只生成计划，绝不下单）。

用途：
- paper 验证通过后，用户授权实盘时，用本脚本生成"精确到数量/方向/保证金"的下单计划；
- 所有订单先过 risk_rules.py 的检查；不通过就输出 NO-GO 与原因；
- 需要用户提供账户并逐笔确认后，才可以用交易所官方界面或另行授权的执行脚本下单。

用法：
    python live_order_plan.py --capital 1000
输出：
    live_plan.json / LIVE_PLAN.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from risk_rules import (  # noqa: E402
    RiskConfig,
    check_entry,
    check_portfolio,
    order_plan,
    size_position,
)

BASE_URL = "https://api.hyperliquid.xyz/info"
OUT_JSON = HERE / "live_plan.json"
OUT_MD = HERE / "LIVE_PLAN.md"

ASSETS = {
    "PURR": {"spot_pair": "PURR/USDC", "perp": "PURR"},
    "HYPE": {"spot_pair": "@107", "perp": "HYPE"},
}

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)


def post_info(body: dict, retries: int = 3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(BASE_URL, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"API 失败: {last}")


def fetch_market() -> dict:
    meta, ctxs = post_info({"type": "metaAndAssetCtxs"})
    names = [u["name"] for u in meta["universe"]]
    idx = {n: i for i, n in enumerate(names)}
    spot_meta, spot_ctxs = post_info({"type": "spotMetaAndAssetCtxs"})
    tokens = {t["index"]: t for t in spot_meta["tokens"]}
    spot_pairs = {p["name"]: p for p in spot_meta["universe"]}
    spot_by_coin: dict[str, dict] = {}
    for c in spot_ctxs:
        prev = spot_by_coin.get(c["coin"])
        if prev is None or float(c.get("dayNtlVlm") or 0) > float(prev.get("dayNtlVlm") or 0):
            spot_by_coin[c["coin"]] = c

    out = {}
    for name, cfg in ASSETS.items():
        u = meta["universe"][idx[cfg["perp"]]]
        pctx = ctxs[idx[cfg["perp"]]]
        pair = spot_pairs[cfg["spot_pair"]]
        base_token = tokens[pair["tokens"][0]]
        sctx = spot_by_coin[cfg["spot_pair"]]
        out[name] = {
            "perp": cfg["perp"],
            "spot_pair": cfg["spot_pair"],
            "spot_mid": float(sctx["midPx"]),
            "perp_mid": float(pctx["midPx"]),
            "funding_rate": float(pctx["funding"]),
            "spot_day_vol_usd": float(sctx.get("dayNtlVlm") or 0),
            "perp_day_vol_usd": float(pctx.get("dayNtlVlm") or 0),
            "spot_sz_decimals": int(base_token.get("szDecimals", 0)),
            "perp_sz_decimals": int(u.get("szDecimals", 0)),
            "max_lev": int(u.get("maxLeverage", 1)),
        }
    return out


def build_plan(market: dict, capital: float) -> dict:
    """纯函数：给定行情快照与资本，生成下单计划（含风控检查结果）。"""
    cfg = RiskConfig(capital_usd=capital)
    positions = []
    plans = []
    rejects: list[str] = []

    for name, m in market.items():
        sizing = size_position(capital, len(market), m["max_lev"], cfg)
        reasons = check_entry(
            name, sizing.notional_usd, m["spot_mid"], m["perp_mid"],
            m["funding_rate"], m["spot_day_vol_usd"], cfg,
        )
        rejects.extend(reasons)
        positions.append({"symbol": name, "notional_usd": sizing.notional_usd})
        plan = order_plan(
            name, sizing.notional_usd, m["spot_mid"], m["perp_mid"],
            m["spot_sz_decimals"], m["perp_sz_decimals"], m["max_lev"],
        )
        plan["sleeve_usd"] = sizing.sleeve_usd
        plan["max_lev"] = m["max_lev"]
        plan["basis_bps"] = round((m["perp_mid"] - m["spot_mid"]) / m["spot_mid"] * 1e4, 2)
        plan["funding_rate_bps_h"] = round(m["funding_rate"] * 1e4, 4)
        plan["spot_day_vol_usd"] = round(m["spot_day_vol_usd"])
        plans.append(plan)

    rejects.extend(check_portfolio(positions, capital, cfg))
    ok = len(rejects) == 0
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "DRY_RUN (no orders placed)",
        "capital_usd": capital,
        "risk_profile": {
            "version": cfg.version,
            "max_total_leverage": cfg.max_total_leverage,
            "max_asset_share": cfg.max_asset_share,
            "max_drawdown_pct": cfg.max_drawdown_pct,
            "max_daily_loss_pct": cfg.max_daily_loss_pct,
            "funding_negative_halt": cfg.funding_negative_halt,
            "max_basis_bps": cfg.max_basis_bps,
            "capacity_frac": cfg.capacity_frac,
        },
        "status": "READY_FOR_AUTHORIZATION" if ok else "NO_GO",
        "rejects": rejects,
        "plans": plans,
        "market_snapshot": market,
    }


def render_md(result: dict) -> str:
    lines = [
        "# 实盘下单计划（DRY RUN — 不会下任何单）",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 资本：**${result['capital_usd']:,.0f}** ｜ 风控版本：{result['risk_profile']['version']}",
        f"- 状态：**{result['status']}**",
        "",
    ]
    if result["rejects"]:
        lines += ["## 拒绝原因（必须先解决）", ""]
        lines += [f"- {r}" for r in result["rejects"]]
        lines += [""]
    lines += [
        "## 订单计划（delta-neutral 建仓）",
        "",
        "| 资产 | 现货 | 现货数量 | 参考价 | 永续 | 永续数量 | 参考价 | 保证金 | basis | 资金费/小时 |",
        "|---|---|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for p in result["plans"]:
        lines.append(
            f"| {p['symbol']} | BUY | {p['spot']['qty']} | {p['spot']['ref_price']} | SELL | "
            f"{p['perp']['qty']} | {p['perp']['ref_price']} | ${p['perp_margin_usd']:,.2f} | "
            f"{p['basis_bps']:+.2f}bps | {p['funding_rate_bps_h']:+.4f}bps |"
        )
    lines += [
        "",
        "## 执行顺序（人工确认后逐步执行）",
        "",
        "1. 现货腿：按计划数量买入（限价或小额市价），成交后确认持仓；",
        "2. 永续腿：同数量名义做空（对冲），确认保证金与杠杆 ≤1x；",
        "3. 两腿成交后核对：现货数量 × 现价 ≈ 永续数量 × 永续价；",
        "4. 记录 entry（价格/数量/时间/手续费）到对账表；",
        "5. 之后每小时核对资金费、每日核对保证金率与回撤。",
        "",
        "## 停止/平仓条件（写死）",
        "",
        "- 组合回撤 ≥ 5% → 全部平仓并停止；",
        "- 单日亏损 ≥ 2% → 当日停止；",
        "- 连续 12 次（约半天）负资金费 → 平仓；",
        "- 资金费/盘口异常（|funding|>50bps/h、|basis|>50bps、容量不足）→ 不建仓/减仓；",
        "- 用户手动喊停（kill switch）→ 立即平仓。",
        "",
        "## 授权要求（必须由用户完成）",
        "",
        "- [ ] 提供交易所账户，API key 仅开通 **交易** 权限，**禁止提现权限**；",
        "- [ ] 逐笔确认上方订单数量与方向；",
        "- [ ] 确认资金规模与最大可承受亏损；",
        "- [ ] 首次实盘建议最小规模（≤ 可承受亏损的 10%），运行 1 周后再评估是否加仓。",
        "",
        "> 本文件由脚本生成，脚本本身不具备下单能力（没有签名、没有私钥、没有下单接口）。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成实盘 dry-run 下单计划（不下单）")
    ap.add_argument("--capital", type=float, default=1000.0, help="实盘资本（USD）")
    args = ap.parse_args()

    market = fetch_market()
    result = build_plan(market, args.capital)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_md(result), encoding="utf-8")
    print(f"状态: {result['status']}  拒绝: {len(result['rejects'])}")
    print(f"已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0 if result["status"] == "READY_FOR_AUTHORIZATION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
