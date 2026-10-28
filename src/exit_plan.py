#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平仓计划生成器（DRY RUN：只生成计划，绝不下单）。

用途：
- paper 验证通过、实盘建仓后，需要平仓/减仓时，用它生成精确的平仓订单；
- 也可作为"紧急退出预案"：提前知道每一步要做什么。

用法：
    python exit_plan.py                          # 用 paper 的 state.json
    python exit_plan.py --positions my_positions.json
    python exit_plan.py --reason emergency       # 紧急退出（标注）
输出：
    exit_plan.json / EXIT_PLAN.md
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
OUT_JSON = HERE / "exit_plan.json"
OUT_MD = HERE / "EXIT_PLAN.md"


def positions_from_paper_state() -> dict:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    positions = []
    for name, p in state["positions"].items():
        positions.append({
            "asset": name,
            "spot_qty": p["qty_spot"],
            "perp_qty": p["qty_perp"],       # 空头数量（正数）
            "spot_ref": p["entry_spot"],
            "perp_ref": p["entry_perp"],
            "max_lev": p["max_lev"],
        })
    return {"capital_usd": state["capital_usd"], "positions": positions,
            "source": "paper_state.json"}


def main() -> int:
    ap = argparse.ArgumentParser(description="生成平仓计划（DRY RUN）")
    ap.add_argument("--positions", default=None, help="自定义持仓 JSON")
    ap.add_argument("--reason", default="manual", choices=["manual", "risk_halt", "emergency"])
    args = ap.parse_args()

    if args.positions:
        src = json.loads(Path(args.positions).read_text(encoding="utf-8"))
    elif STATE.exists():
        src = positions_from_paper_state()
    else:
        print("没有持仓来源（state.json 不存在且未提供 --positions）")
        return 1

    positions = src.get("positions", [])
    problems = []
    orders = []
    for p in positions:
        asset = p["asset"]
        if p["spot_qty"] <= 0 or p["perp_qty"] <= 0:
            problems.append(f"{asset}: 数量无效 spot={p['spot_qty']} perp={p['perp_qty']}")
            continue
        if p["spot_ref"] <= 0 or p["perp_ref"] <= 0:
            problems.append(f"{asset}: 价格无效")
            continue
        orders.append({
            "asset": asset,
            "spot_close": {"side": "SELL", "qty": p["spot_qty"], "ref_price": p["spot_ref"],
                           "notional_usd": round(p["spot_qty"] * p["spot_ref"], 2)},
            "perp_close": {"side": "BUY", "qty": p["perp_qty"], "ref_price": p["perp_ref"],
                           "reduce_only": True, "notional_usd": round(p["perp_qty"] * p["perp_ref"], 2)},
            "max_lev": p.get("max_lev", 1),
        })

    status = "READY" if orders and not problems else ("NO_POSITIONS" if not positions else "INVALID")
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "DRY_RUN (no orders placed)",
        "reason": args.reason,
        "source": src.get("source", args.positions or "unknown"),
        "capital_usd": src.get("capital_usd"),
        "status": status,
        "problems": problems,
        "orders": orders,
    }
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 平仓计划（DRY RUN — 不会下任何单）",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 触发原因：**{args.reason}** ｜ 持仓来源：{result['source']} ｜ 状态：**{status}**",
        "",
    ]
    if problems:
        lines += ["## 问题（需先解决）", ""]
        lines += [f"- {x}" for x in problems]
        lines += [""]
    if orders:
        lines += [
            "## 平仓订单",
            "",
            "| 资产 | 现货 | 数量 | 参考价 | 永续 | 数量 | 参考价 | reduce-only |",
            "|---|---|---:|---:|---|---:|---:|---|",
        ]
        for o in orders:
            lines.append(
                f"| {o['asset']} | {o['spot_close']['side']} | {o['spot_close']['qty']} | "
                f"{o['spot_close']['ref_price']} | {o['perp_close']['side']} | "
                f"{o['perp_close']['qty']} | {o['perp_close']['ref_price']} | ✅ |")
        lines += [
            "",
            "## 执行顺序（务必按顺序，人工确认）",
            "",
            "1. **先平永续腿**（BUY reduce-only，市价/限价）：解除空头保证金占用；",
            "2. **立即平现货腿**（SELL）：此时已无对冲，价格暴露窗口越短越好（建议 30 秒内完成）；",
            "3. 核对：现货持仓 = 0、永续持仓 = 0、保证金释放；",
            "4. 记录成交价、手续费、滑点，写到对账表；",
            "5. 若只成交一腿：立即用市价补齐另一腿（不要留裸腿过夜）。",
            "",
            "> 若为 `emergency`（交易所故障/极端行情）：优先保命——两腿同时市价平仓，接受滑点。",
            "",
        ]
    lines += [
        "## 授权要求",
        "",
        "- [ ] 用户逐笔确认本计划的资产、方向、数量；",
        "- [ ] 确认账户当前持仓与本计划一致（先跑 `reconcile.py`）；",
        "- [ ] 平仓完成后运行 `reconcile.py` 确认两条腿都归零。",
        "",
        "> 本脚本不具备下单能力（无签名、无私钥、无下单接口）。",
        "",
    ]
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"状态: {status}  订单数: {len(orders)}  问题: {len(problems)}")
    print(f"已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0 if status == "READY" else (1 if status == "INVALID" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
