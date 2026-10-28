#!/usr/bin/env python3
"""
GOLDEN TESTS for R156X_BACKTEST_ENGINE_V2 + COST_MODEL
======================================================
覆盖（>=18 项断言，实际 55+）：
  1. 成本模型：三档往返成本 5 / 14 / 26 bps、资金费（默认/序列/方向符号）、
     确认延迟 bar 换算、flat 模型、非法输入 fail-fast；
  2. 执行语义：next-bar open 入场、hold_bars 出场、决策时刻严格早于入场 bar
     （无未来函数）、LONG/SHORT 符号、FLAT 忽略、NO_ENTRY/NO_EXIT 边界；
  3. v1 bug 修复：
     - 持仓到期释放槽位（10 标的 > max_positions=6，v2 全部成交，v1 被永久
       EXPOSURE_LIMIT 卡住）；
     - 跨日 NAV 不重复计入（同一笔跨日持仓，v1 权益曲线重复计入 2 次，
       v2 只计 1 次且逐日 mark-to-market）；
     - 敞口门仍然有效（同时刻 10 标的只放行 6 笔）；
  4. 组合：同 symbol 重叠禁止、不同 symbol 并发、并发持仓权益曲线正确；
  5. 成本模型接入引擎：三档成本逐笔数值、资金费扣减方向、延迟顺延；
  6. v1/v2 逐笔结果对齐（默认成本路径严格复现 v1 的 next-bar open/hold_bars 算术）。

数据：全部为固定 seed 的确定性合成数据（numpy default_rng），不读真实行情。

运行：
  <home>\\AppData\\Local\\Programs\\Python\\Python313\\python.exe \
      01_代码\\scripts\\test_r156x_engine_v2_golden.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):  # pragma: no cover
    pass

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from cost_model import (
    CROSS_VENUE_4LEG_26BPS,
    MAKER_ROUNDTRIP_5BPS,
    PRESETS,
    SINGLE_VENUE_TAKER_14BPS,
    CostModel,
    get_preset,
)
from r156x_backtest_engine_v1 import BacktestEngine as BacktestEngineV1
from r156x_backtest_engine_v2 import BacktestEngineV2

PASS = 0
FAIL = 0
START = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)


def check(name, condition, detail=""):
    """记录一条断言。"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: {detail}")


def section(title):
    print(f"\n{'=' * 72}")
    print(title)
    print(f"{'=' * 72}")


def make_panel(symbols, bars, interval_minutes=60, seed=7, drift=0.0, start_price=10000.0):
    """固定 seed 的确定性合成 panel：symbol x bars 的 OHLC。"""
    rng = np.random.default_rng(seed)
    rows = []
    for idx, sym in enumerate(symbols):
        px = start_price * (1.0 + 0.25 * idx)
        for i in range(bars):
            ts = START + timedelta(minutes=interval_minutes * i)
            if drift:
                px = px * (1.0 + drift) * (1.0 + rng.normal(0.0, 0.0002))
            else:
                px = px * (1.0 + rng.normal(0.0, 0.0015))
            close = px * (1.0 + rng.normal(0.0, 0.0007))
            rows.append(
                {
                    "symbol": sym,
                    "dt": ts,
                    "open": round(px, 6),
                    "high": round(max(px, close) * 1.001, 6),
                    "low": round(min(px, close) * 0.999, 6),
                    "close": round(close, 6),
                }
            )
    return pd.DataFrame(rows)


def sym_panel(panel, symbol):
    return panel[panel["symbol"] == symbol].sort_values("dt").reset_index(drop=True)


def opens_of(panel, symbol):
    return sym_panel(panel, symbol)["open"].to_numpy(dtype=float)


def dt_of(panel, symbol, idx):
    return pd.Timestamp(sym_panel(panel, symbol).iloc[idx]["dt"])


def make_signal(sid, decision_ts, symbol, action="LONG", confidence=80):
    return {
        "snapshot_id": sid,
        "decision_ts": decision_ts,
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
    }


def executed_of(trades):
    return trades[trades["status"] == "EXECUTED"]


# ===========================================================================
# TEST 1: 成本模型单元测试（纯 stdlib 部分）
# ===========================================================================
section("TEST 1: CostModel 预置档位 / 资金费 / 延迟")
check("预置档位数量 = 3", len(PRESETS) == 3, str(sorted(PRESETS)))
check("MAKER 往返 = 5 bps", MAKER_ROUNDTRIP_5BPS.roundtrip_bps == 5.0, MAKER_ROUNDTRIP_5BPS.roundtrip_bps)
check("SINGLE_VENUE_TAKER 往返 = 14 bps", SINGLE_VENUE_TAKER_14BPS.roundtrip_bps == 14.0)
check("CROSS_VENUE_4LEG 往返 = 26 bps", CROSS_VENUE_4LEG_26BPS.roundtrip_bps == 26.0)
check(
    "三档单边成本 = 2.5 / 7 / 13 bps",
    (MAKER_ROUNDTRIP_5BPS.entry_bps, SINGLE_VENUE_TAKER_14BPS.entry_bps, CROSS_VENUE_4LEG_26BPS.entry_bps)
    == (2.5, 7.0, 13.0),
    f"{(MAKER_ROUNDTRIP_5BPS.entry_bps, SINGLE_VENUE_TAKER_14BPS.entry_bps, CROSS_VENUE_4LEG_26BPS.entry_bps)}",
)
check(
    "跨 venue 档位为单边 2 腿（4 腿往返）",
    CROSS_VENUE_4LEG_26BPS.legs_per_side == 2 and CROSS_VENUE_4LEG_26BPS.roundtrip_bps == 4 * 6.5,
)
check("get_preset 大小写不敏感", get_preset("maker_roundtrip_5bps") is MAKER_ROUNDTRIP_5BPS)
check("资金费 12h（默认 1.5bps）= 1.5", MAKER_ROUNDTRIP_5BPS.funding_cost_bps(12, "LONG") == 1.5)
check("资金费 24h LONG = 3.0（多头支付）", MAKER_ROUNDTRIP_5BPS.funding_cost_bps(24, "LONG") == 3.0)
check("资金费 24h SHORT = -3.0（空头收取）", MAKER_ROUNDTRIP_5BPS.funding_cost_bps(24, "SHORT") == -3.0)
check(
    "真实费率序列 [3,1] -> 均值 2bps/12h -> 24h 扣 4bps",
    abs(MAKER_ROUNDTRIP_5BPS.funding_cost_bps(24, "LONG", rates_bps=[3.0, 1.0]) - 4.0) < 1e-12,
)
events_model = CostModel(name="EVENTS", funding_mode="events")
check(
    "events 模式只按完整 12h 计费（25h -> 2 次 = 3bps）",
    abs(events_model.funding_rate_sum_bps(25, rates_bps=[3.0, 1.0]) - 4.0) < 1e-12,
)
check("延迟 12s @1m bar -> 顺延 1 bar", MAKER_ROUNDTRIP_5BPS.entry_delay_bars(1) == 1)
check("延迟 12s @15m bar -> 顺延 1 bar", MAKER_ROUNDTRIP_5BPS.entry_delay_bars(15) == 1)
check("延迟 12s @6s bar -> 顺延 2 bar", MAKER_ROUNDTRIP_5BPS.entry_delay_bars(0.1) == 2)
check("延迟 0s -> 顺延 0 bar", MAKER_ROUNDTRIP_5BPS.with_latency(0.0).entry_delay_bars(1) == 0)
flat20 = CostModel.flat(20)
check("flat(20) 往返 = 20 且无资金费/无延迟", flat20.roundtrip_bps == 20.0 and flat20.funding_bps_per_12h == 0.0 and flat20.confirm_latency_seconds == 0.0)
check("flat(20).net_bps(gross=50) = 30", abs(flat20.net_bps(50.0) - 30.0) < 1e-12)
check("maker.net_bps(gross=50, 24h LONG) = 42", abs(MAKER_ROUNDTRIP_5BPS.net_bps(50.0, "LONG", 24) - 42.0) < 1e-12)
try:
    CostModel(entry_fee_bps=-1.0)
    neg_fee_raised = False
except ValueError:
    neg_fee_raised = True
check("负手续费 fail-fast (ValueError)", neg_fee_raised)
try:
    get_preset("NOT_A_PRESET")
    unknown_raised = False
except KeyError:
    unknown_raised = True
check("未知档位 fail-fast (KeyError)", unknown_raised)

# ===========================================================================
# TEST 2: 执行语义（next-bar open / hold_bars / 无未来函数 / LONG-SHORT / v1 对齐）
# ===========================================================================
section("TEST 2: next-bar open 入场 + hold_bars 出场 + 无未来函数")
panel2 = make_panel(["BTC", "ETH"], 20, seed=7)
btc_open = opens_of(panel2, "BTC")
eth_open = opens_of(panel2, "ETH")

sig_long = make_signal("V2_001", START + timedelta(hours=1), "BTC", "LONG")
engine = BacktestEngineV2(symbols=["BTC", "ETH"], cost_bps=24, hold_bars=2)
trades_long = engine.run([sig_long], panel2)
long_row = executed_of(trades_long).iloc[0]
entry_idx, exit_idx = 2, 4  # 决策 @1h -> 下一根 bar(2h) 入场；hold 2 -> bar 4 出场
expected_entry_px = btc_open[entry_idx]
expected_exit_px = btc_open[exit_idx]
expected_gross = (expected_exit_px - expected_entry_px) / expected_entry_px * 10000.0

check("LONG 成交", long_row["status"] == "EXECUTED", long_row["status"])
check("入场 dt = 决策后第 1 根 bar（2h）", pd.Timestamp(long_row["entry_dt"]) == dt_of(panel2, "BTC", entry_idx))
check("入场价 = 入场 bar 的 open", abs(long_row["entry_px"] - expected_entry_px) < 1e-9)
check("出场 dt = 入场后第 hold_bars 根 bar（4h）", pd.Timestamp(long_row["exit_dt"]) == dt_of(panel2, "BTC", exit_idx))
check("出场价 = 出场 bar 的 open", abs(long_row["exit_px"] - expected_exit_px) < 1e-9)
check("LONG gross 公式正确", abs(long_row["gross_bps"] - round(expected_gross, 2)) < 0.01, long_row["gross_bps"])
check("决策时刻严格早于入场 bar（无未来函数）", pd.Timestamp(long_row["decision_ts"]) < pd.Timestamp(long_row["entry_dt"]))
check(
    "决策 -> 入场至少间隔 1 根 bar（1h）",
    pd.Timestamp(long_row["entry_dt"]) - pd.Timestamp(long_row["decision_ts"]) >= timedelta(hours=1),
)
check(
    "决策时刻在入场 bar 之前（不是同一根 bar）",
    pd.Timestamp(long_row["decision_ts"]) < dt_of(panel2, "BTC", entry_idx),
)

# SHORT 符号 + 成本
sig_short = make_signal("V2_002", START + timedelta(hours=3), "ETH", "SHORT")
engine_s = BacktestEngineV2(symbols=["BTC", "ETH"], cost_bps=24, hold_bars=2)
short_row = executed_of(engine_s.run([sig_short], panel2)).iloc[0]
s_entry, s_exit = eth_open[4], eth_open[6]
expected_gross_s = (s_entry - s_exit) / s_entry * 10000.0
check("SHORT 成交", short_row["status"] == "EXECUTED")
check("SHORT gross 符号正确", abs(short_row["gross_bps"] - round(expected_gross_s, 2)) < 0.01, short_row["gross_bps"])
check("SHORT net = gross - 24（单次扣成本）", abs(short_row["net_bps"] - round(expected_gross_s - 24, 2)) < 0.01)
check("交易记录含成本拆解列（往返 24 = 12+12）", abs(short_row["cost_bps_total"] - 24.0) < 1e-9, short_row["cost_bps_total"])

# FLAT 忽略
sig_flat = [
    make_signal("V2_003A", START + timedelta(hours=1), "BTC", "FLAT"),
    make_signal("V2_003B", START + timedelta(hours=3), "ETH", "SHORT"),
]
flat_trades = BacktestEngineV2(symbols=["BTC", "ETH"], cost_bps=24, hold_bars=2).run(sig_flat, panel2)
flat_exec = executed_of(flat_trades)
check("FLAT 不产生交易", "BTC" not in set(flat_exec["symbol"]))
check("FLAT 场景其余信号仍成交", len(flat_exec) == 1, len(flat_exec))

# NO_ENTRY_BAR / NO_EXIT_BAR 边界
no_entry = BacktestEngineV2(symbols=["BTC", "ETH"], cost_bps=0, hold_bars=2).run(
    [make_signal("V2_004", START + timedelta(hours=19), "BTC", "LONG")], panel2
)
check("最后一根 bar 决策 -> NO_ENTRY_BAR", set(no_entry["status"]) == {"NO_ENTRY_BAR"}, set(no_entry["status"]))
no_exit = BacktestEngineV2(symbols=["BTC", "ETH"], cost_bps=0, hold_bars=4).run(
    [make_signal("V2_005", START + timedelta(hours=17), "BTC", "LONG")], panel2
)
check("hold_bars 超出 panel -> NO_EXIT_BAR", set(no_exit["status"]) == {"NO_EXIT_BAR"}, set(no_exit["status"]))

# v1 / v2 逐笔对齐（默认路径必须严格复现 v1 的入场/出场/毛利/净利）
v1_engine = BacktestEngineV1(symbols=["BTC", "ETH"], cost_bps=24, hold_bars=2)
v1_trade = executed_of(v1_engine.run([sig_long], panel2)).iloc[0]
parity = (
    pd.Timestamp(v1_trade["entry_dt"]) == pd.Timestamp(long_row["entry_dt"])
    and abs(v1_trade["entry_px"] - long_row["entry_px"]) < 1e-12
    and pd.Timestamp(v1_trade["exit_dt"]) == pd.Timestamp(long_row["exit_dt"])
    and abs(v1_trade["exit_px"] - long_row["exit_px"]) < 1e-12
    and abs(v1_trade["gross_bps"] - long_row["gross_bps"]) < 1e-9
    and abs(v1_trade["net_bps"] - long_row["net_bps"]) < 1e-9
)
check("v2 默认路径与 v1 逐笔对齐（入场/出场/毛利/净利）", parity)

# ===========================================================================
# TEST 3: 三档成本接入引擎（数值正确、资金费符号、SHORT 收取）
# ===========================================================================
section("TEST 3: 三档成本数值 + 资金费方向")
zero_model = CostModel(name="ZERO_COST", entry_fee_bps=0.0, exit_fee_bps=0.0, funding_bps_per_12h=1.5)


def single_trade(cost_model, action="LONG", decision_hour=1, symbol="BTC", hold_bars=2):
    eng = BacktestEngineV2(
        symbols=["BTC", "ETH"], hold_bars=hold_bars, cost_model=cost_model, charge_funding=True
    )
    sig = make_signal("COST", START + timedelta(hours=decision_hour), symbol, action)
    return executed_of(eng.run([sig], panel2)).iloc[0]


row_zero = single_trade(zero_model)
row_maker = single_trade(MAKER_ROUNDTRIP_5BPS)
row_taker = single_trade(SINGLE_VENUE_TAKER_14BPS)
row_cross = single_trade(CROSS_VENUE_4LEG_26BPS)
check(
    "引擎接入 MAKER 档：净利比零成本低 5bps",
    abs((row_zero["net_bps"] - row_maker["net_bps"]) - 5.0) <= 0.02,
    f"diff={row_zero['net_bps'] - row_maker['net_bps']:.3f}",
)
check(
    "引擎接入 TAKER 档：净利比零成本低 14bps",
    abs((row_zero["net_bps"] - row_taker["net_bps"]) - 14.0) <= 0.02,
    f"diff={row_zero['net_bps'] - row_taker['net_bps']:.3f}",
)
check(
    "引擎接入 4 腿档：净利比零成本低 26bps",
    abs((row_zero["net_bps"] - row_cross["net_bps"]) - 26.0) <= 0.02,
    f"diff={row_zero['net_bps'] - row_cross['net_bps']:.3f}",
)
gross_2h = float(row_maker["gross_bps"])
check(
    "MAKER LONG 2h 精确净利 = gross - 5 - 0.25（资金费按比例）",
    abs(row_maker["net_bps"] - round(gross_2h - 5.0 - 0.25, 2)) <= 0.011,
    f"got {row_maker['net_bps']} expect {round(gross_2h - 5.0 - 0.25, 2)}",
)
short_maker = single_trade(MAKER_ROUNDTRIP_5BPS, action="SHORT", decision_hour=3, symbol="ETH")
check(
    "MAKER SHORT 2h 精确净利 = gross - 5 + 0.25（资金费收入）",
    abs(short_maker["net_bps"] - round(float(short_maker["gross_bps"]) - 5.0 + 0.25, 2)) <= 0.011,
    f"got {short_maker['net_bps']}",
)
check(
    "引擎记录资金费成本（LONG 2h = 0.25bps）",
    abs(float(row_maker["funding_cost_bps"]) - 0.25) < 1e-6,
    row_maker["funding_cost_bps"],
)

# ===========================================================================
# TEST 4: 【FIX 1】持仓到期释放槽位（10 标的 > max_positions=6）
# ===========================================================================
section("TEST 4: 【FIX 1】持仓到期释放槽位（v1 bug 复现）")
ten = [f"SYM{i:02d}" for i in range(10)]
panel10 = make_panel(ten, 20, seed=11)
signals10 = [make_signal(f"SLOT_{i}", START + timedelta(hours=i), ten[i], "LONG") for i in range(10)]

engine10 = BacktestEngineV2(symbols=ten, hold_bars=1)  # max_positions = 100/(1/6*100) = 6
trades10 = engine10.run(signals10, panel10)
check("v2：10 个标的全部成交（持仓按 hold_bars 过期释放）", len(executed_of(trades10)) == 10, len(executed_of(trades10)))
check(
    "v2：EXPOSURE_LIMIT 为 0（无永久卡死）",
    int((trades10["status"] == "EXPOSURE_LIMIT").sum()) == 0,
    int((trades10["status"] == "EXPOSURE_LIMIT").sum()),
)

v1_engine10 = BacktestEngineV1(symbols=ten, cost_bps=24, hold_bars=1)
v1_trades10 = v1_engine10.run(signals10, panel10)
v1_exec = len(executed_of(v1_trades10))
v1_block = int((v1_trades10["status"] == "EXPOSURE_LIMIT").sum())
check("v1 bug 复现：只有 6 笔成交", v1_exec == 6, v1_exec)
check("v1 bug 复现：4 笔被误判 EXPOSURE_LIMIT", v1_block == 4, v1_block)
check("v1 bug：v2 比 v1 多成交 4 笔", len(executed_of(trades10)) - v1_exec == 4)

# 敞口门本身仍必须有效：同一时刻 10 个标的，只能放行 6 笔
signals_same_ts = [make_signal(f"CONC_{i}", START, ten[i], "LONG") for i in range(10)]
trades_same = BacktestEngineV2(symbols=ten, hold_bars=5).run(signals_same_ts, panel10)
check("敞口上限仍有效：同时刻 10 标的只成交 6 笔", len(executed_of(trades_same)) == 6, len(executed_of(trades_same)))
check(
    "敞口上限仍有效：其余 4 笔 EXPOSURE_LIMIT",
    int((trades_same["status"] == "EXPOSURE_LIMIT").sum()) == 4,
    int((trades_same["status"] == "EXPOSURE_LIMIT").sum()),
)

# ===========================================================================
# TEST 5: 同 symbol 重叠禁止 / 不同 symbol 并发
# ===========================================================================
section("TEST 5: 同 symbol 重叠禁止 + 不同 symbol 并发")
overlap_signals = [
    make_signal("OVL_A", START, "BTC", "LONG"),
    make_signal("OVL_B", START + timedelta(hours=1), "BTC", "LONG"),
    make_signal("OVL_C", START + timedelta(hours=4), "BTC", "LONG"),
]
overlap_trades = BacktestEngineV2(symbols=["BTC", "ETH"], hold_bars=3).run(overlap_signals, panel2)
ovl_exec = executed_of(overlap_trades)
check("同 symbol 持有期内新信号被 OVERLAP_BLOCKED", int((overlap_trades["status"] == "OVERLAP_BLOCKED").sum()) == 1)
check("同 symbol 首笔成交 + 过期后第三笔可成交", len(ovl_exec) == 2, len(ovl_exec))
check(
    "同 symbol 首笔 exit_dt = 决策后 4h（过期边界）",
    pd.Timestamp(ovl_exec.iloc[0]["exit_dt"]) == START + timedelta(hours=4),
)
check(
    "同 symbol 过期后允许再入场（第三笔 entry = 5h）",
    pd.Timestamp(ovl_exec.iloc[1]["entry_dt"]) == START + timedelta(hours=5),
)

conc_signals = [
    make_signal("CONC_BTC", START, "BTC", "LONG"),
    make_signal("CONC_ETH", START, "ETH", "SHORT"),
]
conc_trades = BacktestEngineV2(symbols=["BTC", "ETH"], hold_bars=3).run(conc_signals, panel2)
check("不同 symbol 可并发成交", len(executed_of(conc_trades)) == 2, len(executed_of(conc_trades)))
check("不同 symbol 并发无 OVERLAP_BLOCKED", int((conc_trades["status"] == "OVERLAP_BLOCKED").sum()) == 0)

# ===========================================================================
# TEST 6: 【FIX 2】跨日 NAV 不重复计入（v1 bug 复现）
# ===========================================================================
section("TEST 6: 【FIX 2】跨日 NAV 只计一次（v1 重复计入）")
panel_trend = make_panel(["BTC"], 40, seed=13, drift=0.0008)  # 单调上行，LONG 必然盈利
nav_signal = make_signal("NAV_CROSS_DAY", START, "BTC", "LONG")
nav_engine = BacktestEngineV2(symbols=["BTC"], cost_bps=24, hold_bars=30)
nav_trades = nav_engine.run([nav_signal], panel_trend)
nav_row = executed_of(nav_trades).iloc[0]
weight = nav_engine.position_weight
expected_delta = float(nav_row["net_bps"]) * weight
check(
    "跨日持仓：entry=day0 01:00, exit=day1 07:00（实际跨日）",
    pd.Timestamp(nav_row["entry_dt"]).day == 1 and pd.Timestamp(nav_row["exit_dt"]).day == 2,
    f"{nav_row['entry_dt']} -> {nav_row['exit_dt']}",
)
nav_v2 = nav_engine.compute_nav(nav_trades, initial_capital_bps=10000)
v2_delta = float(nav_v2["equity_bps"].iloc[-1]) - 10000.0
check(
    "v2 跨日权益曲线只累计一次净 PnL",
    abs(v2_delta - expected_delta) <= 0.02,
    f"delta={v2_delta:.4f} expect={expected_delta:.4f}",
)
check(
    "v2 逐日 PnL 之和 = 总净 PnL（无重复、无遗漏）",
    abs(float(nav_v2["daily_pnl_bps"].sum()) - expected_delta) <= 0.05,
    f"sum={nav_v2['daily_pnl_bps'].sum():.4f} expect={expected_delta:.4f}",
)
check("v2 跨日持仓首日 active_positions = 1", int(nav_v2.iloc[0]["active_positions"]) == 1)

# v1 复现：同一笔交易被每个自然日重复计入
v1_nav_engine = BacktestEngineV1(symbols=["BTC"], cost_bps=24, hold_bars=30)
v1_nav_trades = v1_nav_engine.run([nav_signal], panel_trend)
v1_nav = v1_nav_engine.compute_nav(v1_nav_trades, initial_capital_bps=10000)
v1_delta = float(v1_nav["equity_bps"].iloc[-1]) - 10000.0
check(
    "v1 bug 复现：跨日持仓被重复计入约 2 次",
    abs(v1_delta - 2.0 * expected_delta) <= 0.05,
    f"v1_delta={v1_delta:.4f} 2x={2 * expected_delta:.4f}",
)
check(
    "v1 权益曲线相对 v2 高估约 1 倍",
    abs(v1_delta) > abs(v2_delta) * 1.8,
    f"v1={v1_delta:.2f} v2={v2_delta:.2f}",
)

# 无 panel 时线性摊分退化口径：终值仍只计一次
nav_linear = nav_engine.compute_nav(nav_trades, initial_capital_bps=10000, method="linear")
lin_delta = float(nav_linear["equity_bps"].iloc[-1]) - 10000.0
check(
    "linear 退化口径终值同样只计一次",
    abs(lin_delta - expected_delta) <= 0.02,
    f"linear={lin_delta:.4f} expect={expected_delta:.4f}",
)

# ===========================================================================
# TEST 7: 并发持仓权益曲线正确
# ===========================================================================
section("TEST 7: 并发持仓权益曲线")
panel_c = make_panel(["BTC", "ETH"], 40, seed=19)
engine_cross = BacktestEngineV2(symbols=["BTC", "ETH"], cost_bps=24, hold_bars=30)
t_cross = engine_cross.run([make_signal("CONC_NAV_A", START, "BTC", "LONG")], panel_c)
engine_day = BacktestEngineV2(symbols=["BTC", "ETH"], cost_bps=24, hold_bars=3)
t_day = engine_day.run([make_signal("CONC_NAV_B", START, "ETH", "SHORT")], panel_c)
combined = pd.concat([executed_of(t_cross), executed_of(t_day)], ignore_index=True)
nav_concurrent = engine_cross.compute_nav(combined, initial_capital_bps=10000)
expected_conc = float(combined["net_bps"].sum()) * engine_cross.position_weight
conc_delta = float(nav_concurrent["equity_bps"].iloc[-1]) - 10000.0
check("并发持仓：终值 = 各笔 net*weight 之和", abs(conc_delta - expected_conc) <= 0.02, f"{conc_delta:.4f} vs {expected_conc:.4f}")
check("并发持仓：首日 active_positions = 2", int(nav_concurrent.iloc[0]["active_positions"]) == 2, nav_concurrent.iloc[0]["active_positions"])
check(
    "并发持仓：逐日 PnL 之和 = 组合总 PnL",
    abs(float(nav_concurrent["daily_pnl_bps"].sum()) - expected_conc) <= 0.05,
    f"{nav_concurrent['daily_pnl_bps'].sum():.4f}",
)

# ===========================================================================
# TEST 8: 资金费在引擎中的扣减方向
# ===========================================================================
section("TEST 8: 引擎资金费扣减（LONG 支付 / SHORT 收取）")
panel_f = make_panel(["BTC"], 40, seed=23)
fund0 = CostModel(name="FUND_0", funding_bps_per_12h=0.0, confirm_latency_seconds=0.0)
fund15 = CostModel(name="FUND_1.5", funding_bps_per_12h=1.5, confirm_latency_seconds=0.0)


def funding_trade(model, action, funding_rates=None):
    eng = BacktestEngineV2(
        symbols=["BTC"], hold_bars=24, cost_model=model, charge_funding=True,
        funding_rates=funding_rates,
    )
    return executed_of(eng.run([make_signal("FUND", START, "BTC", action)], panel_f)).iloc[0]


long0 = funding_trade(fund0, "LONG")
long15 = funding_trade(fund15, "LONG")
short0 = funding_trade(fund0, "SHORT")
short15 = funding_trade(fund15, "SHORT")
check(
    "LONG 24h：1.5bps/12h 比零资金费少 3bps",
    abs((long0["net_bps"] - long15["net_bps"]) - 3.0) <= 0.02,
    f"diff={long0['net_bps'] - long15['net_bps']:.4f}",
)
check(
    "SHORT 24h：收取资金费，比零资金费多 3bps",
    abs((short15["net_bps"] - short0["net_bps"]) - 3.0) <= 0.02,
    f"diff={short15['net_bps'] - short0['net_bps']:.4f}",
)
long_rates = funding_trade(fund0, "LONG", funding_rates=[3.0, 1.0])
check(
    "引擎接入实际费率序列 [3,1]：24h 扣 4bps",
    abs((long0["net_bps"] - long_rates["net_bps"]) - 4.0) <= 0.02,
    f"diff={long0['net_bps'] - long_rates['net_bps']:.4f}",
)
check("LONG 24h 资金费字段 = 3.0bps", abs(float(long15["funding_cost_bps"]) - 3.0) < 1e-6, long15["funding_cost_bps"])
check("SHORT 24h 资金费字段 = -3.0bps（收入）", abs(float(short15["funding_cost_bps"]) + 3.0) < 1e-6, short15["funding_cost_bps"])

# ===========================================================================
# TEST 9: 确认延迟 12s -> 入场顺延 bar
# ===========================================================================
section("TEST 9: 确认延迟 12s -> 入场顺延")
base_cm = CostModel.flat(0, name="NO_LATENCY")
latency_cm = CostModel.flat(0, name="LATENCY_12S", confirm_latency_seconds=12.0)
sig_lat = make_signal("LAT", START + timedelta(hours=1), "BTC", "LONG")
base_trade = executed_of(
    BacktestEngineV2(symbols=["BTC", "ETH"], hold_bars=2, cost_model=base_cm).run([sig_lat], panel2)
).iloc[0]
lat_trade = executed_of(
    BacktestEngineV2(
        symbols=["BTC", "ETH"], hold_bars=2, cost_model=latency_cm, apply_confirm_latency=True
    ).run([sig_lat], panel2)
).iloc[0]
check(
    "12s 延迟 @1h bar：入场顺延 1 根 bar",
    pd.Timestamp(lat_trade["entry_dt"]) - pd.Timestamp(base_trade["entry_dt"]) == timedelta(hours=1),
    f"{base_trade['entry_dt']} -> {lat_trade['entry_dt']}",
)
check(
    "顺延后入场价 = 顺延后 bar 的 open",
    abs(lat_trade["entry_px"] - btc_open[3]) < 1e-12,
    f"got {lat_trade['entry_px']} expect {btc_open[3]}",
)
check("交易记录标注 entry_delay_bars = 1", int(lat_trade["entry_delay_bars"]) == 1)
check("延迟场景仍无未来函数（decision < entry）", pd.Timestamp(lat_trade["decision_ts"]) < pd.Timestamp(lat_trade["entry_dt"]))

panel_m1 = make_panel(["BTC", "ETH"], 60, interval_minutes=1, seed=17)
m1_open = opens_of(panel_m1, "BTC")
sig_m1 = make_signal("LAT_M1", START, "BTC", "LONG")
m1_base = executed_of(
    BacktestEngineV2(symbols=["BTC"], hold_bars=5, cost_model=base_cm).run([sig_m1], panel_m1)
).iloc[0]
m1_lat = executed_of(
    BacktestEngineV2(
        symbols=["BTC"], hold_bars=5, cost_model=latency_cm, apply_confirm_latency=True
    ).run([sig_m1], panel_m1)
).iloc[0]
check(
    "12s 延迟 @1m bar：入场从 1m 顺延到 2m",
    pd.Timestamp(m1_lat["entry_dt"]) - pd.Timestamp(m1_base["entry_dt"]) == timedelta(minutes=1),
    f"{m1_base['entry_dt']} -> {m1_lat['entry_dt']}",
)
check("1m bar 顺延后入场价 = open[2]", abs(m1_lat["entry_px"] - m1_open[2]) < 1e-12, m1_lat["entry_px"])
check("1m bar 场景 entry_delay_bars = 1", int(m1_lat["entry_delay_bars"]) == 1)

# ===========================================================================
# TEST 10: summary 指标完整性（v1 字段 + v2 成本字段）
# ===========================================================================
section("TEST 10: summary 指标")
summary_engine = BacktestEngineV2(
    symbols=["BTC", "ETH"], hold_bars=2, cost_model=MAKER_ROUNDTRIP_5BPS, charge_funding=True
)
summary_trades = summary_engine.run(
    [make_signal("SUM_1", START + timedelta(hours=1), "BTC", "LONG")], panel2
)
summary_nav = summary_engine.compute_nav(summary_trades, initial_capital_bps=1000)
summary = summary_engine.summary(summary_trades, summary_nav)
v1_keys = {
    "n_trades", "total_net_bps", "avg_net_bps", "avg_gross_bps", "pf", "win_rate",
    "max_dd_weighted_bps", "profitable_symbols", "total_symbols", "symbol_pnl",
    "overlap_blocked", "exposure_blocked",
}
check("summary 保留 v1 全部字段", v1_keys.issubset(set(summary)), sorted(v1_keys - set(summary)))
check("summary 记录成本模型名", summary["cost_model"] == "MAKER_ROUNDTRIP_5BPS", summary.get("cost_model"))
check("summary 总成本 = 往返 5 + 资金费 0.25", abs(summary["total_cost_bps"] - 5.25) <= 0.02, summary.get("total_cost_bps"))
check("summary 含 NAV 最大回撤字段", "nav_max_dd_bps" in summary)
check("summary 含逐笔数 = 1", summary["n_trades"] == 1)

# ===========================================================================
# 结果
# ===========================================================================
section("GOLDEN TEST RESULTS (R156X ENGINE V2)")
print(f"  PASS : {PASS}")
print(f"  FAIL : {FAIL}")
print(f"  TOTAL: {PASS + FAIL}")
if FAIL > 0:
    print(f"\n  >>> {FAIL} 项失败，禁止用于生产回测 <<<")
    sys.exit(1)
print("\n  >>> ALL TESTS PASSED：v1 两个 bug 已修复，成本模型接入正确 <<<")
