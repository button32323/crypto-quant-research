"""
R156X_BACKTEST_ENGINE_V2
========================
基于 v1（``r156x_backtest_engine_v1.py``，保持冻结、未修改）的分支版本。

相对 v1 的修复与增强
--------------------
FIX 1 —— 持仓到期释放槽位（v1 实质 bug）
    v1 的 ``active_positions`` 只在同 symbol 新信号到来时才删除，从不按
    ``hold_bars`` 过期清理；当标的数 > ``max_positions`` 时，第 max_positions+1 个
    标的起永久返回 ``EXPOSURE_LIMIT``，敞口上限门实际失效。
    v2 在每次决策时刻与入场时刻都按持仓的真实区间 ``[entry_dt, exit_dt)``
    清理已到期持仓（``_purge_expired``），并改用"入场时仍在持有的持仓数"做
    敞口检查；同时保留"同 symbol 禁止重叠、不同 symbol 可并发"两条规则。

FIX 2 —— 逐日 mark-to-market 权益曲线（v1 实质 bug）
    v1 ``compute_nav`` 对每个自然日把"当日仍持有"的整笔 ``net_bps * weight``
    加进 equity，跨 N 天的持仓被计入 N 次，NAV / 最大回撤 / 年化系统性偏高。
    v2 改为按日累计的 mark-to-market：每天用该日边界（UTC 零点）前最后一根
    bar 的收盘价对未平仓头寸估值，已平仓头寸计入完整净 PnL，成本在入场日/
    出场日各计一次。因此任一交易对权益曲线累计贡献严格等于其 ``net_bps *
    position_weight``，且只出现一次。无 bar 价格时退化为按时间比例线性摊分
    （``method='linear'``），仍然只计一次。

FIX/ADD 3 —— 统一成本模型接入
    ``cost_model`` 可传 ``CostModel`` 实例 / 预置档位名（str）/ dict。
    - 手续费 + 滑点按单边收取（入场、出场各一次），等价 v1 的"单次扣成本"；
    - 资金费按持有小时数扣减（正费率多头支付、空头收取，带符号）；
    - ``apply_confirm_latency=True`` 时把 ``confirm_latency_seconds``（默认 12s）
      按 bar 粒度折算成顺延 bar 数（``CostModel.entry_delay_bars``），入场
      价格取顺延后那根 bar 的 open，避免"信号确认前成交"的乐观偏差。
    - 不传 ``cost_model`` 时退化为 ``cost_bps`` 单常数成本（v1 语义，资金费=0、
      无延迟），用于历史结果复现与 v1/v2 对照。

其余保持 v1 语义
----------------
- ``t`` 决策 → 严格晚于 decision_ts 的下一根 bar ``open`` 成交（无未来函数）；
- 固定 ``hold_bars`` 后那根 bar 的 ``open`` 出场（v2 不按资金费/延迟改变出场时点）；
- LONG/SHORT 毛利符号正确；
- 单次成本扣除（往返成本只扣一次，不按日重复）；
- 同 symbol 不重叠、账户总敞口 <= ``max_concurrent_pct``；
- ``status`` 取值与 v1 一致：EXECUTED / OVERLAP_BLOCKED / EXPOSURE_LIMIT /
  NO_ENTRY_BAR / NO_EXIT_BAR。

v1/v2 API 差异（兼容性）
------------------------
- v1: ``BacktestEngine(symbols, cost_bps=24, ...)``
- v2: ``BacktestEngineV2(symbols, cost_bps=24, ..., cost_model=None, ...)``
  其余位置参数顺序完全一致，``run()`` / ``compute_nav()`` / ``summary()``
  签名兼容（``compute_nav`` 增加可选 ``panel`` / ``method`` 参数）。
- v2 交易记录新增列：``entry_cost_bps`` / ``exit_cost_bps`` /
  ``funding_cost_bps`` / ``cost_bps_total`` / ``hold_hours`` /
  ``entry_delay_bars`` / ``cost_model``。

使用示例
--------
>>> from cost_model import SINGLE_VENUE_TAKER_14BPS
>>> engine = BacktestEngineV2(
...     symbols=['BTC', 'ETH'], hold_bars=4,
...     cost_model=SINGLE_VENUE_TAKER_14BPS,
...     apply_confirm_latency=True, charge_funding=True,
... )
>>> trades = engine.run(signals, panel)          # signals: list[dict], panel: DataFrame
>>> nav = engine.compute_nav(trades)             # 逐日 mark-to-market 权益曲线
>>> metrics = engine.summary(trades, nav)
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

try:  # 直接以脚本方式运行 / 同目录 import
    from cost_model import CostModel, get_preset
except ImportError:  # pragma: no cover - 被当包 import 时的兜底
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cost_model import CostModel, get_preset

__all__ = ["BacktestEngineV2"]

_VALID_ACTIONS = frozenset({"LONG", "SHORT"})
_ENTRY_PRICE_COLUMN = "open"

#: 交易记录列（保证 EXECUTED 与 blocked 行拥有同一 schema）
_TRADE_COLUMNS = [
    "snapshot_id",
    "symbol",
    "action",
    "confidence",
    "decision_ts",
    "overlap_blocked",
    "entry_dt",
    "entry_px",
    "exit_dt",
    "exit_px",
    "gross_bps",
    "net_bps",
    "status",
    "entry_cost_bps",
    "exit_cost_bps",
    "funding_cost_bps",
    "cost_bps_total",
    "hold_hours",
    "entry_delay_bars",
    "cost_model",
]


class BacktestEngineV2:
    """执行引擎 v2：修复 v1 两个 bug，并接入统一成本模型。

    Parameters
    ----------
    symbols:
        允许交易的标的列表（与 panel 的 symbol 匹配）；内部排序后使用。
    cost_bps:
        单常数往返成本（bps）。仅当 ``cost_model is None`` 时生效，
        语义与 v1 完全一致（``net_bps = gross_bps - cost_bps``）。
    hold_bars:
        固定持有 bar 数，出场价 = 入场 bar 后第 ``hold_bars`` 根 bar 的 open。
    max_concurrent_pct:
        账户总敞口上限（%），默认 100。
    entry_offset:
        入场相对"决策后第一根 bar"的偏移 bar 数，默认 1：
        1 = 下一根 bar open（v1 语义）；>1 表示再顺延。确认延迟另由
        ``apply_confirm_latency`` 叠加。
    position_weight:
        单笔仓位权重，默认 1/6。
    cost_model:
        ``CostModel`` 实例 / 预置档位名（如 ``'SINGLE_VENUE_TAKER_14BPS'``）/ dict。
        ``None`` 时使用 ``CostModel.flat(cost_bps)``（v1 兼容，无资金费/无延迟）。
    apply_confirm_latency:
        是否把 ``cost_model.confirm_latency_seconds`` 折算为入场顺延 bar 数。
        默认 False（严格复现 v1 的 next-bar open 时点）；接入真实成本口径时建议 True。
    bar_interval_minutes:
        bar 周期（分钟）。``None`` 时从 panel 的 dt 中位数自动推断。
    charge_funding:
        是否按持有小时数扣减资金费；仅在使用非零资金费的 cost_model 时生效。
    funding_rates:
        实际资金费率序列（bps / 12h），可为 ``Sequence``（全局）或
        ``Mapping[str, Sequence]``（按 symbol）。``None`` 时用 cost_model 默认费率。
    """

    def __init__(
        self,
        symbols: Sequence[str],
        cost_bps: float = 24.0,
        hold_bars: int = 12,
        max_concurrent_pct: float = 100.0,
        entry_offset: int = 1,
        position_weight: float = 1 / 6,
        cost_model: CostModel | str | Mapping[str, Any] | None = None,
        apply_confirm_latency: bool = False,
        bar_interval_minutes: float | None = None,
        charge_funding: bool = True,
        funding_rates: Sequence[float] | Mapping[str, Sequence[float]] | None = None,
    ) -> None:
        self.symbols = sorted(str(s) for s in symbols)
        self.cost_bps = float(cost_bps)
        self.hold_bars = int(hold_bars)
        self.max_concurrent_pct = float(max_concurrent_pct)
        self.entry_offset = int(entry_offset)
        self.position_weight = float(position_weight)
        self.apply_confirm_latency = bool(apply_confirm_latency)
        self.bar_interval_minutes = (
            None if bar_interval_minutes is None else float(bar_interval_minutes)
        )
        self.charge_funding = bool(charge_funding)
        self.funding_rates = funding_rates
        self.cost_model = self._resolve_cost_model(cost_model)
        #: 是否走 v1 兼容的单常数成本路径（无资金费、无延迟）
        self.legacy_flat_cost = cost_model is None
        self.max_positions = int(self.max_concurrent_pct / (self.position_weight * 100.0))
        self._validate()

        # 运行时状态
        self._active: dict[str, dict[str, Any]] = {}
        self._panel: pd.DataFrame | None = None
        self._sym_panels: dict[str, pd.DataFrame] = {}
        self._interval_minutes: float = self.bar_interval_minutes or 60.0
        self._latency_bars: int = 0
        self._last_trades: pd.DataFrame | None = None
        self._mark_cache: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

    # ------------------------------------------------------------------ 初始化
    def _resolve_cost_model(
        self, cost_model: CostModel | str | Mapping[str, Any] | None
    ) -> CostModel:
        """把 ``cost_model`` 参数归一成 CostModel 实例。"""
        if cost_model is None:
            # v1 兼容：往返 cost_bps、无资金费、无延迟
            return CostModel.flat(
                self.cost_bps,
                name="LEGACY_FLAT_V1_COMPAT",
                funding_bps_per_12h=0.0,
                confirm_latency_seconds=0.0,
            )
        if isinstance(cost_model, CostModel):
            return cost_model
        if isinstance(cost_model, str):
            return get_preset(cost_model)
        if isinstance(cost_model, Mapping):
            return CostModel(**dict(cost_model))
        raise TypeError(
            "cost_model must be a CostModel, preset name (str), mapping, or None; "
            f"got {type(cost_model).__name__}"
        )

    def _validate(self) -> None:
        if self.cost_bps < 0:
            raise ValueError("cost_bps must be non-negative")
        if self.hold_bars < 1:
            raise ValueError("hold_bars must be >= 1")
        if not (0 < self.max_concurrent_pct <= 100):
            raise ValueError("max_concurrent_pct must be in (0, 100]")
        if self.position_weight <= 0 or self.position_weight > 1:
            raise ValueError("position_weight must be in (0, 1]")
        if self.entry_offset < 1:
            raise ValueError("entry_offset must be >= 1")
        if self.bar_interval_minutes is not None and self.bar_interval_minutes <= 0:
            raise ValueError("bar_interval_minutes must be positive")

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _infer_bar_minutes(panel: pd.DataFrame) -> float:
        """从 panel 的 dt 中位数推断 bar 周期（分钟），失败时回退 60。"""
        try:
            dt_series = pd.to_datetime(panel["dt"], utc=True)
            naive = (
                dt_series.dt.tz_convert("UTC")
                .dt.tz_localize(None)
                .dropna()
                .unique()
            )
            dts = np.sort(np.asarray(naive, dtype="datetime64[ns]"))
            if len(dts) < 2:
                return 60.0
            deltas = np.diff(dts).astype("timedelta64[s]").astype(float)
            deltas = deltas[deltas > 0]
            if len(deltas) == 0:
                return 60.0
            return float(np.median(deltas)) / 60.0
        except (TypeError, ValueError):  # pragma: no cover - 防御式
            return 60.0

    @staticmethod
    def _direction_sign(action: str) -> int:
        return 1 if action == "LONG" else -1

    def _rates_for(self, symbol: str) -> Sequence[float] | None:
        """取该 symbol 的实际资金费率序列（如有）。"""
        rates = self.funding_rates
        if rates is None:
            return None
        if isinstance(rates, Mapping):
            if symbol in rates:
                return rates[symbol]
            return rates.get("*")
        return rates  # type: ignore[return-value]

    def _purge_expired(self, now_ts: pd.Timestamp) -> list[str]:
        """【FIX 1】释放 hold_end_ts <= now_ts 的持仓槽位。"""
        expired = [sym for sym, pos in self._active.items() if pos["hold_end_ts"] <= now_ts]
        for sym in expired:
            del self._active[sym]
        return expired

    def _base_row(self, sig: Mapping[str, Any], decision_ts: pd.Timestamp) -> dict[str, Any]:
        """构造与 EXECUTED 行同 schema 的空交易记录。"""
        row: dict[str, Any] = {col: None for col in _TRADE_COLUMNS}
        row.update(
            {
                "snapshot_id": sig.get("snapshot_id", ""),
                "symbol": sig.get("symbol", ""),
                "action": str(sig.get("action", "")).upper().strip(),
                "confidence": sig.get("confidence", 0),
                "decision_ts": decision_ts,
                "overlap_blocked": False,
                "gross_bps": 0.0,
                "net_bps": 0.0,
                "entry_delay_bars": self._latency_bars,
                "cost_model": self.cost_model.name,
            }
        )
        return row

    # ------------------------------------------------------------------ 主流程
    def run(
        self,
        signals: Sequence[Mapping[str, Any]] | pd.DataFrame,
        panel: pd.DataFrame,
    ) -> pd.DataFrame:
        """执行回测，返回逐笔交易记录 DataFrame。

        Parameters
        ----------
        signals:
            每条 dict 含 ``snapshot_id`` / ``decision_ts`` / ``symbol`` /
            ``action``(LONG/SHORT/FLAT) / ``confidence``。FLAT 被忽略。
        panel:
            DataFrame，至少含 ``['symbol', 'dt', 'open']``；1h/15m/1m bar 均可。

        Returns
        -------
        DataFrame
            一行一笔信号（含被拒单），``status`` 语义同 v1，附加成本拆解列。
        """
        if not isinstance(panel, pd.DataFrame):
            raise TypeError("panel must be a DataFrame")
        required_cols = {"symbol", "dt", _ENTRY_PRICE_COLUMN}
        missing = required_cols - set(panel.columns)
        if missing:
            raise ValueError(f"panel missing required columns: {sorted(missing)}")

        panel = panel.copy()
        panel["dt"] = pd.to_datetime(panel["dt"], utc=True)
        panel = panel.sort_values(["symbol", "dt"]).reset_index(drop=True)
        self._panel = panel
        self._sym_panels = {
            str(sym): grp.sort_values("dt").reset_index(drop=True)
            for sym, grp in panel.groupby("symbol", sort=True)
        }
        self._interval_minutes = (
            self.bar_interval_minutes
            if self.bar_interval_minutes is not None
            else self._infer_bar_minutes(panel)
        )
        self._latency_bars = (
            self.cost_model.entry_delay_bars(self._interval_minutes)
            if self.apply_confirm_latency
            else 0
        )
        self._active = {}
        self._mark_cache = {}

        # ---- 信号标准化（FLAT 忽略；按决策时刻稳定排序，同时保留输入顺序作为 tie-break）
        if isinstance(signals, pd.DataFrame):
            sig_df = signals.copy()
        else:
            sig_df = pd.DataFrame(list(signals))
        if sig_df.empty or "action" not in sig_df.columns:
            self._last_trades = pd.DataFrame(columns=_TRADE_COLUMNS)
            return self._last_trades
        sig_df["action"] = sig_df["action"].astype(str).str.upper().str.strip()
        sig_df = sig_df[sig_df["action"].isin(_VALID_ACTIONS)].copy()
        if sig_df.empty:
            self._last_trades = pd.DataFrame(columns=_TRADE_COLUMNS)
            return self._last_trades
        sig_df["decision_ts"] = pd.to_datetime(sig_df["decision_ts"], utc=True)
        sig_df["_input_order"] = np.arange(len(sig_df))
        sig_df = sig_df.sort_values(
            ["decision_ts", "_input_order"], kind="mergesort"
        ).reset_index(drop=True)

        trades: list[dict[str, Any]] = []
        total_latency = self.entry_offset - 1 + self._latency_bars

        for _, sig in sig_df.iterrows():
            sym = str(sig["symbol"]).strip()
            if sym not in self._sym_panels:
                continue
            decision_ts = pd.Timestamp(sig["decision_ts"])
            act = str(sig["action"])
            row = self._base_row(sig, decision_ts)

            # ---- (1) FIX 1：决策时刻先释放已到期槽位
            self._purge_expired(decision_ts)

            # ---- (2) 同 symbol 不允许重叠（与 v1 语义一致：decision_ts < 前一笔 exit_dt 即拦截）
            existing = self._active.get(sym)
            if existing is not None:
                row["overlap_blocked"] = True
                row["status"] = "OVERLAP_BLOCKED"
                trades.append(row)
                continue

            # ---- (3) 定位入场 bar：严格晚于 decision_ts 的下一根 bar（无未来函数）
            sym_panel = self._sym_panels[sym]
            dts = sym_panel["dt"]
            mask = (dts > decision_ts).to_numpy()
            if not mask.any():
                row["status"] = "NO_ENTRY_BAR"
                trades.append(row)
                continue
            first_pos = int(mask.argmax())
            entry_pos = first_pos + total_latency
            if entry_pos >= len(sym_panel):
                row["status"] = "NO_ENTRY_BAR"
                trades.append(row)
                continue

            entry_row = sym_panel.iloc[entry_pos]
            entry_px = float(entry_row[_ENTRY_PRICE_COLUMN])
            entry_dt = pd.Timestamp(entry_row["dt"])
            if not math.isfinite(entry_px) or entry_px <= 0:
                row["status"] = "NO_ENTRY_BAR"
                trades.append(row)
                continue

            # ---- (4) 定位出场 bar：entry_pos + hold_bars 的 open
            exit_pos = entry_pos + self.hold_bars
            if exit_pos >= len(sym_panel):
                row["entry_dt"] = entry_dt
                row["entry_px"] = entry_px
                row["status"] = "NO_EXIT_BAR"
                trades.append(row)
                continue

            exit_row = sym_panel.iloc[exit_pos]
            exit_px = float(exit_row[_ENTRY_PRICE_COLUMN])
            exit_dt = pd.Timestamp(exit_row["dt"])

            # ---- (5) FIX 1：按入场时刻再释放一次槽位，然后检查真实并发持仓数
            self._purge_expired(entry_dt)
            if len(self._active) >= self.max_positions:
                row["entry_dt"] = entry_dt
                row["entry_px"] = entry_px
                row["status"] = "EXPOSURE_LIMIT"
                trades.append(row)
                continue

            # ---- (6) 成本 / 资金费 / PnL
            if act == "LONG":
                gross_bps = (exit_px - entry_px) / entry_px * 10000.0
            else:  # SHORT
                gross_bps = (entry_px - exit_px) / entry_px * 10000.0
            hold_hours = max(0.0, (exit_dt - entry_dt).total_seconds() / 3600.0)
            entry_cost_bps = self.cost_model.entry_bps
            exit_cost_bps = self.cost_model.exit_bps
            funding_cost_bps = (
                self.cost_model.funding_cost_bps(
                    hold_hours, act, rates_bps=self._rates_for(sym)
                )
                if self.charge_funding
                else 0.0
            )
            net_bps = gross_bps - entry_cost_bps - exit_cost_bps - funding_cost_bps

            # ---- (7) 登记持仓（真实持有区间 [entry_dt, exit_dt)，供后续到期释放/敞口判断）
            self._active[sym] = {
                "snapshot_id": sig.get("snapshot_id", ""),
                "action": act,
                "decision_ts": decision_ts,
                "entry_dt": entry_dt,
                "entry_pos": entry_pos,
                "entry_px": entry_px,
                "hold_end_ts": exit_dt,
                "cost_model": self.cost_model.name,
            }

            row.update(
                {
                    "entry_dt": entry_dt,
                    "entry_px": entry_px,
                    "exit_dt": exit_dt,
                    "exit_px": exit_px,
                    "gross_bps": float(round(gross_bps, 2)),
                    "net_bps": float(round(net_bps, 2)),
                    "status": "EXECUTED",
                    "entry_cost_bps": round(entry_cost_bps, 4),
                    "exit_cost_bps": round(exit_cost_bps, 4),
                    "funding_cost_bps": round(funding_cost_bps, 6),
                    "cost_bps_total": round(entry_cost_bps + exit_cost_bps + funding_cost_bps, 6),
                    "hold_hours": round(hold_hours, 6),
                }
            )
            trades.append(row)

        result = pd.DataFrame(trades, columns=_TRADE_COLUMNS) if trades else pd.DataFrame(columns=_TRADE_COLUMNS)
        self._last_trades = result
        return result

    # ------------------------------------------------------------- 权益曲线
    def _build_mark_cache(self, panel: pd.DataFrame) -> None:
        """构建按 symbol 的 (dt_naive_ns, close, open) 数组，供逐日 mark-to-market 取价。"""
        panel = panel.copy()
        panel["dt"] = pd.to_datetime(panel["dt"], utc=True)
        price_col = "close" if "close" in panel.columns else _ENTRY_PRICE_COLUMN
        cache: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for sym, grp in panel.groupby("symbol", sort=True):
            grp = grp.sort_values("dt")
            naive = (
                pd.to_datetime(grp["dt"], utc=True)
                .dt.tz_convert("UTC")
                .dt.tz_localize(None)
                .to_numpy()
                .astype("datetime64[ns]")
            )
            close = pd.to_numeric(grp[price_col], errors="coerce").to_numpy(dtype=float)
            open_ = pd.to_numeric(grp[_ENTRY_PRICE_COLUMN], errors="coerce").to_numpy(dtype=float)
            cache[str(sym)] = (naive, close, open_)
        self._mark_cache = cache

    def _mark_price(self, symbol: str, ts: pd.Timestamp) -> float | None:
        """取严格早于 ``ts`` 的最后一根 bar 的收盘价（无 close 时用 open）。

        使用 ``dt < ts``（而不是 <=）：bar 的 close 发生在 dt+interval，
        当日界（UTC 零点）时点应使用前一小时 bar 的 close 作为 mark。
        """
        cache = self._mark_cache.get(symbol)
        if cache is None:
            return None
        dts, close, open_ = cache
        ts_ns = np.datetime64(
            pd.Timestamp(ts).tz_convert("UTC").tz_localize(None), "ns"
        )
        pos = int(np.searchsorted(dts, ts_ns, side="left")) - 1
        if pos < 0:
            return None
        value = close[pos]
        if not math.isfinite(value) or value <= 0:
            value = open_[pos]
        if not math.isfinite(value) or value <= 0:
            return None
        return float(value)

    def compute_nav(
        self,
        trades_df: pd.DataFrame,
        initial_capital_bps: float = 0.0,
        panel: pd.DataFrame | None = None,
        method: str = "auto",
        floor_bps: float | None = None,
    ) -> pd.DataFrame:
        """【FIX 2】生成逐日 mark-to-market 权益曲线（每笔 PnL 只计一次）。

        Parameters
        ----------
        trades_df:
            ``run()`` 的返回值（含被拒单行也可）。
        initial_capital_bps:
            起始权益（bps），默认 0。
        panel:
            用于取每日 mark 价格的行情面板；``None`` 时用 ``run()`` 时保存的面板。
        method:
            ``'auto'``（默认，有 panel 用 mark_to_market，否则 linear）/
            ``'mark_to_market'`` / ``'linear'``（按时间比例摊分，仍只计一次）。
        floor_bps:
            可选权益下限（v1 的 ``initial_capital_bps`` 地板语义）；
            ``None``（默认）不做人为截断，以便最大回撤反映真实亏损。

        Returns
        -------
        DataFrame
            列：date / equity_bps / drawdown_bps / active_positions / daily_pnl_bps。
        """
        if trades_df is None or not isinstance(trades_df, pd.DataFrame) or trades_df.empty:
            return pd.DataFrame()
        executed = trades_df[trades_df["status"] == "EXECUTED"].copy()
        if executed.empty:
            return pd.DataFrame()
        executed["entry_dt"] = pd.to_datetime(executed["entry_dt"], utc=True)
        executed["exit_dt"] = pd.to_datetime(executed["exit_dt"], utc=True)
        executed = executed.dropna(subset=["entry_dt", "exit_dt", "entry_px", "exit_px", "net_bps"])

        panel_use = panel if panel is not None else self._panel
        if method == "auto":
            use_method = "mark_to_market" if panel_use is not None else "linear"
        else:
            use_method = str(method).lower()
            if use_method not in {"mark_to_market", "linear"}:
                raise ValueError("method must be 'auto', 'mark_to_market' or 'linear'")
            if use_method == "mark_to_market" and panel_use is None:
                raise ValueError("mark_to_market NAV requires a panel (pass panel= or call run() first)")
        if use_method == "mark_to_market":
            self._build_mark_cache(panel_use)  # type: ignore[arg-type]

        weight = self.position_weight
        trades = executed.to_dict("records")
        start_day = executed["entry_dt"].min().floor("D")
        end_day = executed["exit_dt"].max().ceil("D")
        all_days = pd.date_range(start=start_day, end=end_day, freq="D", tz="UTC")

        nav_rows: list[dict[str, Any]] = []
        for day in all_days:
            day_end = day + pd.Timedelta(days=1)
            equity = float(initial_capital_bps)
            n_active = 0
            for tr in trades:
                entry_dt = tr["entry_dt"]
                exit_dt = tr["exit_dt"]
                if entry_dt >= day_end:
                    continue
                if exit_dt > day:
                    n_active += 1
                net_bps = float(tr["net_bps"])
                if exit_dt <= day_end:
                    # 当日边界前已平仓：整笔净 PnL 只在此计入一次
                    equity += net_bps * weight
                elif use_method == "linear":
                    # 无行情时的退化口径：按持有时间比例线性摊分（累计仍只计一次）
                    total = (exit_dt - entry_dt).total_seconds()
                    if total <= 0:
                        continue
                    progress = (day_end - entry_dt).total_seconds() / total
                    equity += net_bps * weight * min(1.0, max(0.0, progress))
                else:
                    # 仍持仓：按日界 mark price 估值未实现 PnL（已扣入场成本）
                    entry_px = float(tr["entry_px"])
                    mark_px = self._mark_price(str(tr["symbol"]), day_end)
                    if mark_px is None:
                        mark_px = entry_px
                    sign = self._direction_sign(str(tr["action"]))
                    marked_gross = sign * (mark_px - entry_px) / entry_px * 10000.0
                    entry_cost = float(tr.get("entry_cost_bps") or 0.0)
                    equity += (marked_gross - entry_cost) * weight
            nav_rows.append(
                {
                    "date": day,
                    "equity_bps": round(equity, 2),
                    "active_positions": n_active,
                }
            )

        nav_df = pd.DataFrame(nav_rows)
        equity_values = nav_df["equity_bps"].to_numpy(dtype=float)
        if floor_bps is not None:
            equity_values = np.maximum(equity_values, float(floor_bps))
            nav_df["equity_bps"] = np.round(equity_values, 2)
        # 最大回撤以起始权益为初始峰值，避免漏掉"从 0 开始的亏损"
        running_max = np.maximum.accumulate(
            np.concatenate(([float(initial_capital_bps)], equity_values))
        )[1:]
        nav_df["drawdown_bps"] = np.round(running_max - equity_values, 2)
        daily = np.diff(np.concatenate(([float(initial_capital_bps)], equity_values)))
        nav_df["daily_pnl_bps"] = np.round(daily, 2)
        return nav_df[["date", "equity_bps", "drawdown_bps", "active_positions", "daily_pnl_bps"]]

    # ------------------------------------------------------------------ 汇总
    def summary(self, trades_df: pd.DataFrame, nav_df: pd.DataFrame | None = None) -> dict[str, Any]:
        """逐笔口径的汇总指标（保留 v1 全部字段，并新增成本/资金费拆解）。"""
        if trades_df is None or not isinstance(trades_df, pd.DataFrame) or trades_df.empty:
            return {"n_trades": 0, "status": "NO_TRADES"}
        executed = trades_df[trades_df["status"] == "EXECUTED"].copy()
        if executed.empty:
            return {"n_trades": 0, "status": "NO_TRADES"}

        n = len(executed)
        net = executed["net_bps"].to_numpy(dtype=float)
        gross = executed["gross_bps"].to_numpy(dtype=float)
        wins = int((net > 0).sum())
        losses = int((net <= 0).sum())
        total_gain = net[net > 0].sum() if wins > 0 else 1e-10
        total_loss = abs(net[net <= 0].sum()) if losses > 0 else 1e-10
        pf = total_gain / total_loss if total_loss > 1e-9 else float("inf")

        cum = np.cumsum(net * self.position_weight)
        running_max = np.maximum.accumulate(cum)
        max_dd = float((running_max - cum).max()) if len(cum) else 0.0
        sym_pnl = executed.groupby("symbol")["net_bps"].sum().to_dict()

        metrics: dict[str, Any] = {
            "n_trades": n,
            "total_net_bps": round(float(net.sum()), 2),
            "avg_net_bps": round(float(net.mean()), 2),
            "avg_gross_bps": round(float(gross.mean()), 2),
            "pf": round(float(pf), 2),
            "win_rate": round(wins / n, 4) if n > 0 else 0,
            "max_dd_weighted_bps": round(max_dd, 2),
            "profitable_symbols": sum(1 for v in sym_pnl.values() if v > 0),
            "total_symbols": len(sym_pnl),
            "symbol_pnl": {s: round(float(v), 2) for s, v in sym_pnl.items()},
            "overlap_blocked": int((trades_df["status"] == "OVERLAP_BLOCKED").sum()),
            "exposure_blocked": int((trades_df["status"] == "EXPOSURE_LIMIT").sum()),
            "n_long": int((executed["action"] == "LONG").sum()),
            "n_short": int((executed["action"] == "SHORT").sum()),
            "cost_model": self.cost_model.name,
            "cost_model_breakdown": self.cost_model.as_dict(),
            "hold_bars": self.hold_bars,
            "max_positions": self.max_positions,
        }
        for column, key in (
            ("cost_bps_total", "total_cost_bps"),
            ("funding_cost_bps", "total_funding_cost_bps"),
        ):
            if column in executed.columns:
                metrics[key] = round(float(executed[column].fillna(0.0).sum()), 4)
        if "cost_bps_total" in executed.columns:
            metrics["avg_cost_bps"] = round(
                float(executed["cost_bps_total"].fillna(0.0).mean()), 4
            )
        if nav_df is not None and not nav_df.empty and "drawdown_bps" in nav_df.columns:
            metrics["nav_max_dd_bps"] = round(float(nav_df["drawdown_bps"].max()), 2)
            metrics["nav_final_equity_bps"] = round(float(nav_df["equity_bps"].iloc[-1]), 2)
        return metrics
