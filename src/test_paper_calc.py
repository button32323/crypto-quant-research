#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paper trading 计算逻辑单测（不联网）。

覆盖：
- 资金费累计 / 结算统计 / 连续负资金费 streak
- NAV 公式
- 风控状态生成（stop signal）
- 日志追加与 schema 一致性
运行：python test_paper_calc.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import paper_hl_carry as ph  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}  {detail}")


def base_state() -> dict:
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capital_usd": 1000.0,
        "initial_nav_usd": 996.56,
        "funding_accrued_usd": 0.0,
        "last_funding_ts_ms": {"PURR": 3_600_000, "HYPE": 3_600_000},
        "entry_cost_usd": 3.44,
        "snapshots": 0,
        "positions": {
            "PURR": {"spot_coin": "PURR/USDC", "perp": "PURR", "max_lev": 3,
                     "sleeve_usd": 500.0, "notional_usd": 375.0,
                     "entry_spot": 0.0945, "entry_perp": 0.0947,
                     "entry_basis_bps": 21.2, "qty_spot": 375 / 0.0945,
                     "qty_perp": 375 / 0.0947, "entry_cost_usd": 1.556},
            "HYPE": {"spot_coin": "@107", "perp": "HYPE", "max_lev": 10,
                     "sleeve_usd": 500.0, "notional_usd": 454.55,
                     "entry_spot": 76.1, "entry_perp": 76.05,
                     "entry_basis_bps": -6.6, "qty_spot": 454.55 / 76.1,
                     "qty_perp": 454.55 / 76.05, "entry_cost_usd": 1.886},
        },
    }


CTX = {
    "PURR": {"spot_mid": 0.0945, "perp_mid": 0.0947, "basics_bps": 21.2,
             "funding_rate": 0.0001, "spot_day_ntl": 500000.0, "perp_day_ntl": 1e6, "max_lev": 3},
    "HYPE": {"spot_mid": 76.1, "perp_mid": 76.05, "basics_bps": -6.6,
             "funding_rate": -0.00005, "spot_day_ntl": 1e8, "perp_day_ntl": 1e8, "max_lev": 10},
}


def funding_rows(coin: str) -> list[dict]:
    if coin == "PURR":
        return [{"fundingRate": "0.0001", "time": 7_200_001},
                {"fundingRate": "0.0001", "time": 10_800_001}]
    return [{"fundingRate": "-0.00005", "time": 7_200_002},
            {"fundingRate": "-0.00005", "time": 10_800_002}]


def main() -> int:
    # --- 1) 资金费累计 / 统计 / streak ---
    state = base_state()
    with mock.patch.object(ph, "fetch_funding_since", side_effect=lambda coin, start: funding_rows(coin)):
        metrics = ph.compute_nav(state, CTX)

    expected_funding = 0.0002 * 375.0 - 0.0001 * 454.55
    check("资金费累计正确", abs(metrics["funding_accrued_total"] - expected_funding) < 1e-6,
          f"{metrics['funding_accrued_total']} vs {expected_funding}")
    check("结算次数=4", metrics["funding_settlements"] == 4, str(metrics["funding_settlements"]))
    check("正费率次数=2", metrics["funding_positive"] == 2, str(metrics["funding_positive"]))
    check("PURR streak=0", metrics["funding_streaks"]["PURR"] == 0, str(metrics["funding_streaks"]))
    check("HYPE streak=2", metrics["funding_streaks"]["HYPE"] == 2, str(metrics["funding_streaks"]))
    check("last funding ts 已推进", state["last_funding_ts_ms"]["PURR"] >= 10_800_000,
          str(state["last_funding_ts_ms"]))
    # 逐笔结算审计日志
    plog = state.get("settlements_log", {}).get("PURR", [])
    hlog = state.get("settlements_log", {}).get("HYPE", [])
    check("PURR 结算审计 2 条", len(plog) == 2, str(len(plog)))
    check("HYPE 结算审计 2 条", len(hlog) == 2, str(len(hlog)))
    check("PURR 结算审计金额正确",
          abs(plog[0]["usd"] - 0.0001 * 375.0) < 1e-9 if plog else False, str(plog[:1]))

    expected_nav = 1000 - 3.44 + expected_funding
    check("NAV 公式正确", abs(metrics["nav"] - expected_nav) < 0.001, f"{metrics['nav']} vs {expected_nav}")

    # --- 1b) 重复时间行只计一次 ---
    state_dup = base_state()
    def dup_rows(coin, start):
        base = funding_rows(coin)
        return base + [dict(r) for r in base]  # 时间重复、金额重复
    with mock.patch.object(ph, "fetch_funding_since", side_effect=dup_rows):
        m_dup = ph.compute_nav(state_dup, CTX)
    check("重复结算行只计一次", abs(m_dup["funding_accrued_total"] - expected_funding) < 1e-6,
          f"{m_dup['funding_accrued_total']} vs {expected_funding}")
    check("重复行不计入结算次数", m_dup["funding_settlements"] == 4, str(m_dup["funding_settlements"]))

    # --- 1c) 追赶场景：runner 漏跑 5 小时，一次补齐 5 个结算 ---
    state_catch = base_state()
    def catch_rows(coin, start):
        if coin == "PURR":
            return [{"fundingRate": "0.0001", "time": 7_200_000 + i * 3_600_000} for i in range(5)]
        return [{"fundingRate": "-0.0001", "time": 7_200_000 + i * 3_600_000} for i in range(5)]
    with mock.patch.object(ph, "fetch_funding_since", side_effect=catch_rows):
        m_catch = ph.compute_nav(state_catch, CTX)
    check("追赶场景结算次数=10", m_catch["funding_settlements"] == 10, str(m_catch["funding_settlements"]))
    expected_catch = 5 * 0.0001 * 375.0 - 5 * 0.0001 * 454.55
    check("追赶场景资金费金额正确", abs(m_catch["funding_accrued_total"] - expected_catch) < 1e-6,
          f"{m_catch['funding_accrued_total']} vs {expected_catch}")
    check("追赶场景 streak 正确（PURR=0/HYPE=5）",
          m_catch["funding_streaks"] == {"PURR": 0, "HYPE": 5}, str(m_catch["funding_streaks"]))

    # --- 2) streak 遇到正费率清零 ---
    state2 = base_state()
    def rows_reset(coin, start):
        if coin == "PURR":
            return [{"fundingRate": "-0.001", "time": 7_200_001},
                    {"fundingRate": "-0.001", "time": 10_800_001},
                    {"fundingRate": "0.0002", "time": 14_400_001}]
        return [{"fundingRate": "0.0001", "time": 7_200_002}]
    with mock.patch.object(ph, "fetch_funding_since", side_effect=rows_reset):
        m2 = ph.compute_nav(state2, CTX)
    check("streak 遇正费率清零", m2["funding_streaks"]["PURR"] == 0, str(m2["funding_streaks"]))

    # --- 3) 连续负资金费触发停止信号 ---
    state3 = base_state()
    state3["funding_streaks"] = {"PURR": 0, "HYPE": 12}
    with mock.patch.object(ph, "LOG_PATH", Path(tempfile.gettempdir()) / "__no_such_log__.csv"):
        lines = ph.build_risk_section(state3, {"nav": 1000.0, "funding_streaks": {"HYPE": 12},
                                               "max_negative_streak": 12})
    text = "\n".join(lines)
    check("风控段包含连续负资金费行", "连续负资金费" in text, text[:80])
    check("风控段标注停止信号", "停止信号" in text and "有" in text, text[:120])

    # --- 4) append_log schema 与日志写入 ---
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "paper_log.csv"
    state4 = base_state()
    ctx4 = CTX
    with mock.patch.object(ph, "LOG_PATH", log), \
         mock.patch.object(ph, "STATE_PATH", tmp / "state.json"), \
         mock.patch.object(ph, "REPORT_PATH", tmp / "REPORT.md"), \
         mock.patch.object(ph, "fetch_funding_since", side_effect=lambda coin, start: []):
        ph.append_log(state4, ctx4)
        ph.append_log(state4, ctx4)
    lines_csv = log.read_text(encoding="utf-8").splitlines()
    check("日志有表头+2行", len(lines_csv) == 3, str(len(lines_csv)))
    header = lines_csv[0].split(",")
    check("日志列数 16", len(header) == 16, str(len(header)))
    check("日志列名含 funding_streaks 无关字段", "funding_settlements" in header)
    check("REPORT 已生成", (tmp / "REPORT.md").exists())
    check("state.json 已生成", (tmp / "state.json").exists())

    print(f"\nPASS={PASS} FAIL={FAIL} TOTAL={PASS+FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())




