#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paper trading：Hyperliquid 现货-永续资金费 carry（可执行子集 PURR + HYPE）。

目的：为「通过交易加密货币获得收入」目标做 30 天 paper 验证（不动真钱）。
策略：买入现货 + 做空等名义永续（delta-neutral），收取资金费，赚 basis 收敛。
口径（与 R174A SPEC 一致）：
- tier-0 taker：永续 0.045%、现货 0.070%（每次成交）
- 滑点：每笔成交 15 bps（往返 4 次 fill）
- 建仓成本率 = 0.045% + 0.070% + 2×0.15% = 0.415% × 名义
- 资本分配：每个资产 sleeve = capital/2；notional = sleeve / (1 + 1/maxLev)（含永续保证金）
用法：
    python paper_hl_carry.py --init --capital 1000   # 初始化（Day 0 建仓）
    python paper_hl_carry.py --run                   # 每次运行：更新净值、累计资金费、追加日志
    python paper_hl_carry.py --status                # 查看当前状态
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from risk_rules import RiskConfig, drawdown_pct, should_halt  # noqa: E402
except Exception:  # pragma: no cover - 风控模块缺失时不影响 paper 主流程
    RiskConfig = None  # type: ignore[assignment]
    should_halt = None  # type: ignore[assignment]
    drawdown_pct = None  # type: ignore[assignment]

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "paper_log.csv"
REPORT_PATH = HERE / "REPORT.md"

BASE_URL = "https://api.hyperliquid.xyz/info"
TIMEOUT = 25

# 清空死代理环境变量（本机 HTTP(S)_PROXY 指向未监听的 <local-proxy>）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

ASSETS: dict[str, dict] = {
    "PURR": {"spot_coin": "PURR/USDC", "perp": "PURR", "max_lev": 3},
    "HYPE": {"spot_coin": "@107", "perp": "HYPE", "max_lev": 10},
}
ENTRY_COST_RATE = 0.00045 + 0.00070 + 2 * 0.0015  # 0.415% of notional


def post_info(body: dict, retries: int = 3) -> dict | list:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                BASE_URL,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API 请求失败: {last_err}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_ms() -> int:
    return int(time.time() * 1000)


def fetch_contexts() -> dict[str, dict]:
    """返回每个资产的当前现货/永续中间价、资金费率与成交额。"""
    meta, ctxs = post_info({"type": "metaAndAssetCtxs"})
    names = [u["name"] for u in meta["universe"]]
    idx = {n: i for i, n in enumerate(names)}
    spot_meta, spot_ctxs = post_info({"type": "spotMetaAndAssetCtxs"})
    spot_by_coin = {}
    for c in spot_ctxs:
        # 同一 coin 若有多条，保留成交额最大的一条
        prev = spot_by_coin.get(c["coin"])
        if prev is None or float(c.get("dayNtlVlm") or 0) > float(prev.get("dayNtlVlm") or 0):
            spot_by_coin[c["coin"]] = c

    out: dict[str, dict] = {}
    for name, cfg in ASSETS.items():
        pctx = ctxs[idx[cfg["perp"]]]
        sctx = spot_by_coin[cfg["spot_coin"]]
        spot_mid = float(sctx["midPx"])
        perp_mid = float(pctx["midPx"])
        out[name] = {
            "spot_mid": spot_mid,
            "perp_mid": perp_mid,
            "basics_bps": (perp_mid - spot_mid) / spot_mid * 1e4,
            "funding_rate": float(pctx["funding"]),
            "spot_day_ntl": float(sctx.get("dayNtlVlm") or 0),
            "perp_day_ntl": float(pctx.get("dayNtlVlm") or 0),
            "max_lev": cfg["max_lev"],
        }
    return out


def fetch_funding_since(perp: str, start_ms: int) -> list[dict]:
    rows = post_info({"type": "fundingHistory", "coin": perp, "startTime": start_ms})
    return [r for r in (rows or []) if int(r["time"]) > start_ms]


def load_state() -> dict:
    if not STATE_PATH.exists():
        raise SystemExit("未初始化：先运行 python paper_hl_carry.py --init --capital 1000")
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    state["last_update_ts"] = now_iso()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def init_state(capital: float) -> dict:
    ctx = fetch_contexts()
    sleeve = capital / len(ASSETS)
    positions = {}
    total_cost = 0.0
    for name, cfg in ASSETS.items():
        notional = sleeve / (1 + 1 / cfg["max_lev"])
        m = ctx[name]
        cost = notional * ENTRY_COST_RATE
        total_cost += cost
        spot_mid = m["spot_mid"]
        perp_mid = m["perp_mid"]
        positions[name] = {
            "spot_coin": cfg["spot_coin"],
            "perp": cfg["perp"],
            "max_lev": cfg["max_lev"],
            "sleeve_usd": round(sleeve, 2),
            "notional_usd": round(notional, 2),
            "entry_spot": spot_mid,
            "entry_perp": perp_mid,
            "entry_basis_bps": round((perp_mid - spot_mid) / spot_mid * 1e4, 2),
            "qty_spot": notional / spot_mid,
            "qty_perp": notional / perp_mid,
            "entry_cost_usd": round(cost, 4),
        }
    state = {
        "created_at": now_iso(),
        "capital_usd": capital,
        "initial_nav_usd": round(capital - total_cost, 2),
        "funding_accrued_usd": 0.0,
        "last_funding_ts_ms": {cfg["perp"]: now_ms() for cfg in ASSETS.values()},
        "positions": positions,
        "entry_cost_usd": round(total_cost, 4),
        "snapshots": 0,
    }
    save_state(state)
    append_log(state, ctx, first=True)
    return state


def compute_nav(state: dict, ctx: dict) -> dict:
    funding_accrued = 0.0
    per_asset = {}
    for name, pos in state["positions"].items():
        cfg = ASSETS[name]
        start_ms = int(state["last_funding_ts_ms"][cfg["perp"]])
        rows = fetch_funding_since(cfg["perp"], start_ms)
        # 去重保护：API 若返回重复时点，只计一次
        uniq = {}
        for r in rows:
            uniq[int(r["time"])] = r
        rows = [uniq[t] for t in sorted(uniq)]
        new_funding = sum(float(r["fundingRate"]) * pos["notional_usd"] for r in rows)
        funding_accrued += new_funding
        stats = state.setdefault("funding_stats", {}).setdefault(
            name, {"settlements": 0, "positive": 0})
        stats["settlements"] += len(rows)
        stats["positive"] += sum(1 for r in rows if float(r["fundingRate"]) > 0)
        if rows:
            ordered = sorted(rows, key=lambda r: int(r["time"]))
            streak = state.setdefault("funding_streaks", {}).setdefault(name, 0)
            for r in ordered:
                streak = streak + 1 if float(r["fundingRate"]) < 0 else 0
            state["funding_streaks"][name] = streak
            state["last_funding_ts_ms"][cfg["perp"]] = int(ordered[-1]["time"])
            # 逐笔结算审计日志（保留最近 200 条/资产）
            logs = state.setdefault("settlements_log", {})
            entries = logs.setdefault(name, [])
            for r in ordered:
                rate = float(r["fundingRate"])
                entries.append({"time": int(r["time"]), "rate": rate,
                                "usd": round(rate * pos["notional_usd"], 8)})
            logs[name] = entries[-200:]

        m = ctx[name]
        spot_pnl = pos["qty_spot"] * (m["spot_mid"] - pos["entry_spot"])
        perp_pnl = pos["qty_perp"] * (pos["entry_perp"] - m["perp_mid"])
        basis_pnl = spot_pnl + perp_pnl
        per_asset[name] = {
            "spot_mid": m["spot_mid"],
            "perp_mid": m["perp_mid"],
            "basis_bps": round(m["basics_bps"], 2),
            "spot_pnl": round(spot_pnl, 4),
            "perp_pnl": round(perp_pnl, 4),
            "basis_pnl": round(basis_pnl, 4),
            "new_funding": round(new_funding, 6),
            "funding_rate": m["funding_rate"],
            "expected_funding_per_hour": round(m["funding_rate"] * pos["notional_usd"], 6),
        }

    state["funding_accrued_usd"] = state["funding_accrued_usd"] + funding_accrued
    basis_pnl_total = sum(v["basis_pnl"] for v in per_asset.values())
    nav = state["capital_usd"] - state["entry_cost_usd"] + state["funding_accrued_usd"] + basis_pnl_total
    ret = (nav / state["capital_usd"] - 1) * 100
    created = datetime.fromisoformat(state["created_at"])
    elapsed_h = max((datetime.now(timezone.utc) - created).total_seconds() / 3600, 1e-9)
    ann = ret * (365 * 24 / elapsed_h)
    return {
        "per_asset": per_asset,
        "funding_accrued_total": round(state["funding_accrued_usd"], 6),
        "basis_pnl_total": round(basis_pnl_total, 4),
        "entry_cost_total": round(state["entry_cost_usd"], 4),
        "nav": round(nav, 4),
        "ret_pct": round(ret, 4),
        "elapsed_hours": round(elapsed_h, 3),
        "annualized_pct_naive": round(ann, 2) if elapsed_h >= 24 else None,
        "funding_settlements": sum(v["settlements"] for v in state.get("funding_stats", {}).values()),
        "funding_positive": sum(v["positive"] for v in state.get("funding_stats", {}).values()),
        "funding_positive_share": (
            sum(v["positive"] for v in state.get("funding_stats", {}).values())
            / max(1, sum(v["settlements"] for v in state.get("funding_stats", {}).values()))
        ),
        "funding_streaks": dict(state.get("funding_streaks", {})),
        "max_negative_streak": max(state.get("funding_streaks", {}).values(), default=0),
        "expected_funding_per_hour": round(
            sum(v["expected_funding_per_hour"] for v in per_asset.values()), 6),
        "expected_annualized_pct": round(
            sum(v["expected_funding_per_hour"] for v in per_asset.values())
            * 24 * 365 / state["capital_usd"] * 100, 2),
    }


def append_log(state: dict, ctx: dict, first: bool = False) -> dict:
    metrics = compute_nav(state, ctx)
    row = {
        "ts": now_iso(),
        "capital_usd": state["capital_usd"],
        "nav_usd": metrics["nav"],
        "ret_pct": metrics["ret_pct"],
        "funding_accrued_usd": metrics["funding_accrued_total"],
        "basis_pnl_usd": metrics["basis_pnl_total"],
        "entry_cost_usd": metrics["entry_cost_total"],
        "elapsed_hours": metrics["elapsed_hours"],
        "annualized_pct_naive": metrics["annualized_pct_naive"],
    }
    for name, v in metrics["per_asset"].items():
        row[f"{name}_basis_bps"] = v["basis_bps"]
        row[f"{name}_funding_rate"] = v["funding_rate"]
    row["funding_settlements"] = metrics["funding_settlements"]
    row["funding_positive"] = metrics["funding_positive"]
    row["funding_positive_share"] = round(metrics["funding_positive_share"], 4)

    import csv
    fieldnames = list(row.keys())
    if LOG_PATH.exists():
        with LOG_PATH.open(encoding="utf-8", newline="") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header != fieldnames:
            raise RuntimeError(
                "paper_log.csv 列不一致，请先运行一次修复（或删除该文件后重新 --init）。"
            )
    with LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0:
            writer.writeheader()
        writer.writerow(row)
    state["snapshots"] = int(state.get("snapshots", 0)) + 1
    state["expected_funding_per_hour"] = metrics["expected_funding_per_hour"]
    save_state(state)
    write_report(state, metrics)
    return metrics


def _read_log_navs() -> list[float]:
    navs: list[float] = []
    if LOG_PATH.exists():
        import csv
        with LOG_PATH.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                try:
                    navs.append(float(r["nav_usd"]))
                except Exception:  # noqa: BLE001
                    pass
    return navs


def build_risk_section(state: dict, metrics: dict) -> list[str]:
    """风控状态表（使用 risk_rules.py 的规则；paper 阶段只告警，不平仓）。"""
    if RiskConfig is None or should_halt is None or drawdown_pct is None:
        return ["", "## 风控状态", "", "- risk_rules 模块不可用", ""]
    cfg = RiskConfig(capital_usd=state["capital_usd"])
    navs = _read_log_navs() + [metrics["nav"]]
    dd = drawdown_pct(navs)
    today = now_iso()[:10]
    navs_today = []
    if LOG_PATH.exists():
        import csv
        with LOG_PATH.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if str(r.get("ts", "")).startswith(today):
                    try:
                        navs_today.append(float(r["nav_usd"]))
                    except Exception:  # noqa: BLE001
                        pass
    daily_pnl = (metrics["nav"] / navs_today[0] - 1) * 100 if navs_today else 0.0
    halt, reasons = should_halt(navs, daily_pnl, metrics.get("max_negative_streak", 0), cfg)
    ok = lambda b: "✅" if b else "❌"
    streaks = metrics.get("funding_streaks", {})
    streak_txt = ", ".join(f"{k}={v}" for k, v in streaks.items()) or "0"
    return [
        "",
        f"## 风控状态（risk_rules v{cfg.version}）",
        "",
        "| 检查 | 当前 | 规则 | 判定 |",
        "|---|---:|---|---|",
        f"| 组合回撤 | {dd:.3f}% | < {cfg.max_drawdown_pct:.0f}% | {ok(dd < cfg.max_drawdown_pct)} |",
        f"| 当日盈亏 | {daily_pnl:+.3f}% | > -{cfg.max_daily_loss_pct:.0f}% | {ok(daily_pnl > -cfg.max_daily_loss_pct)} |",
        f"| 连续负资金费 | {streak_txt} | < {cfg.funding_negative_halt} 次 | {ok(metrics.get('max_negative_streak', 0) < cfg.funding_negative_halt)} |",
        f"| 停止信号 | {'有' if halt else '无'} | — | {ok(not halt)} |",
        "",
        f"- 停止原因：{'；'.join(reasons) if reasons else '无'}",
        "- 说明：paper 阶段风控只告警、不自动平仓；实盘执行时由 risk_rules 输出停止/平仓指令。",
        "",
    ]


def _env_percentile(annualized_pct: float) -> str:
    """当前隐含年化在历史 30 天滚动窗口中的分位（读 funding_env_distribution.json）。"""
    path = Path(__file__).resolve().parent / "funding_env_distribution.json"
    if not path.exists():
        return "—"
    try:
        q = json.loads(path.read_text(encoding="utf-8"))["quantiles_p0_to_p100"]
        idx = sum(1 for v in q if v <= annualized_pct) - 1
        if idx < 0:
            pct = 0.0
        elif idx >= len(q) - 1:
            pct = 100.0
        else:
            lo, hi = q[idx], q[idx + 1]
            frac = (annualized_pct - lo) / (hi - lo) if hi > lo else 0.0
            pct = idx + max(0.0, min(1.0, frac))
        return f"第 {pct:.1f}% 分位"
    except Exception:  # noqa: BLE001
        return "—"


def write_report(state: dict, metrics: dict) -> None:
    lines = [
        "# Paper Trading 日报 — HL 现货-永续 Carry（PURR + HYPE）",
        "",
        f"- 更新时间：{now_iso()}",
        f"- 虚拟本金：${state['capital_usd']:,.2f} ｜ 已运行：{metrics['elapsed_hours']:.1f} 小时 ｜ 快照数：{state['snapshots']}",
        "",
        "## 账户",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 当前净值 | ${metrics['nav']:,.2f} |",
        f"| 累计收益率 | {metrics['ret_pct']:+.4f}% |",
        f"| 资金费累计 | ${metrics['funding_accrued_total']:+.4f} |",
        f"| Basis 盈亏 | ${metrics['basis_pnl_total']:+.4f} |",
        f"| 建仓成本 | ${metrics['entry_cost_total']:.4f} |",
        f"| 资金费结算次数 / 正费率占比 | {metrics['funding_settlements']} 次 / {metrics['funding_positive_share']:.1%} |",
        f"| 当前费率预期 | ${metrics['expected_funding_per_hour']:+.4f}/小时"
        f"（按当前费率年化 {metrics['expected_annualized_pct']:+.2f}%，验证门槛 >10%） |",
        f"| 当前环境分位（历史30天窗口） | {_env_percentile(metrics['expected_annualized_pct'])} |",
        f"| 年化（运行不足 24h 不显示；线性折算仅供参考） | {metrics['annualized_pct_naive'] if metrics['annualized_pct_naive'] is not None else '—'} |",
        "",
        "## 持仓",
        "",
        "| 资产 | 名义 | 建仓 basis | 当前 basis | Spot 盈亏 | Perp 盈亏 | 资金费 | 当前费率 | 预期$/h |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, pos in state["positions"].items():
        v = metrics["per_asset"][name]
        lines.append(
            f"| {name} | ${pos['notional_usd']:,.0f} | {pos['entry_basis_bps']:+.1f} bps | "
            f"{v['basis_bps']:+.1f} bps | {v['spot_pnl']:+.4f} | {v['perp_pnl']:+.4f} | "
            f"{v['new_funding']:+.6f} | {v['funding_rate']:+.6%}/h | "
            f"${v['expected_funding_per_hour']:+.5f} |"
        )
    lines += [
        "",
        "## 最近结算（逐笔审计）",
        "",
        "| 资产 | 时间(UTC) | 费率 | 资金费($) |",
        "|---|---|---:|---:|",
    ]
    for name in state["positions"]:
        for e in state.get("settlements_log", {}).get(name, [])[-3:]:
            ts = datetime.fromtimestamp(e["time"] / 1000, tz=timezone.utc).strftime("%m-%d %H:%M")
            lines.append(f"| {name} | {ts} | {e['rate'] * 1e4:+.4f} bps | ${e['usd']:+.6f} |")
    lines += [
        "",
        "## 收入投影（按年化 10–20% 区间，仅供参考）",
        "",
        "| 资本 | 年收入 @10% | 年收入 @15% | 年收入 @20% |",
        "|---|---:|---:|---:|",
        "| $1,000 | $100 | $150 | $200 |",
        "| $5,000 | $500 | $750 | $1,000 |",
        "| $10,000 | $1,000 | $1,500 | $2,000 |",
        "| $50,000 | $5,000 | $7,500 | $10,000 |",
    ]
    lines += build_risk_section(state, metrics)
    lines += [
        "",
        "## 说明",
        "",
        "- 这是 **paper trading**：全部为虚拟资金，不涉及任何真实下单或转账。",
        "- 成本口径：tier-0 taker（perp 0.045% / spot 0.070%）+ 每 fill 15bps 滑点；建仓成本 0.415% × 名义。",
        "- 资金费按 Hyperliquid 官方每小时结算费率累计（正费率时永续空头收取）。",
        "- 目标：累计运行 30 天后，检查实现收益率、最大回撤、资金费连续性，再决定是否进入小资金实盘（实盘需用户逐笔授权）。",
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def status() -> None:
    if not STATE_PATH.exists():
        raise SystemExit("未初始化。")
    state = load_state()
    ctx = fetch_contexts()
    metrics = compute_nav(state, ctx)
    print(json.dumps({"state": state, "metrics": metrics}, ensure_ascii=False, indent=2)[:4000])


def main() -> int:
    ap = argparse.ArgumentParser(description="HL carry paper trading")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--capital", type=float, default=1000.0)
    args = ap.parse_args()
    if args.init:
        if STATE_PATH.exists():
            print("state.json 已存在；如需重新初始化请先备份/删除")
            return 1
        state = init_state(args.capital)
        print(f"初始化完成：虚拟本金 ${args.capital:,.2f}，净值 ${state['initial_nav_usd']:,.2f}")
        print(f"状态: {STATE_PATH}")
        print(f"日志: {LOG_PATH}")
        print(f"报告: {REPORT_PATH}")
        return 0
    if args.status:
        status()
        return 0
    if args.run or len(sys.argv) == 1:
        state = load_state()
        ctx = fetch_contexts()
        metrics = append_log(state, ctx)
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())











