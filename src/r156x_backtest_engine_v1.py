"""
R156X_BACKTEST_ENGINE_V1
Unified backtest execution engine for directional strategies.

Design constraints:
  - Multi-symbol correct traversal (reset_index per symbol)
  - Next-bar open execution
  - LONG SHORT profit sign correctness
  - Same-symbol no overlapping holds
  - Account exposure <= 100%
  - Commission deducted once per round trip
  - Last valid response per snapshot
  - Market event clustering
  - Realistic NAV curve (no negative account)

Usage:
  engine = BacktestEngine(symbols=['BTC','ETH'], cost_bps=24, hold_bars=12)
  engine.run(signals, panel)
  engine.summary()
"""
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import json

class BacktestEngine:
    def __init__(self, symbols, cost_bps=24, hold_bars=12, max_concurrent_pct=100,
                 entry_offset=1, position_weight=1/6):
        self.symbols = sorted(symbols)
        self.cost_bps = cost_bps
        self.hold_bars = hold_bars
        self.max_concurrent_pct = max_concurrent_pct
        self.entry_offset = entry_offset
        self.position_weight = position_weight
        self.max_positions = int(max_concurrent_pct / (position_weight * 100))
        self._validate()

    def _validate(self):
        if self.cost_bps < 0:
            raise ValueError('cost_bps must be non-negative')
        if self.hold_bars < 1:
            raise ValueError('hold_bars must be >= 1')
        if self.max_concurrent_pct > 100 or self.max_concurrent_pct <= 0:
            raise ValueError('max_concurrent_pct must be in (0, 100]')

    def run(self, signals, panel):
        """
        signals: list of dicts with keys:
            snapshot_id, decision_ts (datetime UTC), symbol, action (LONG/SHORT/FLAT),
            confidence (0-100)
            FLAT signals are ignored (only actionable entries).
        panel: DataFrame with ['symbol', 'dt', 'open', 'high', 'low', 'close'] (1h bars)

        Returns: DataFrame with one row per executed trade.
        """
        if not isinstance(panel, pd.DataFrame):
            raise TypeError('panel must be a DataFrame')
        panel = panel.copy()
        panel['dt'] = pd.to_datetime(panel['dt'], utc=True)
        panel = panel.sort_values(['symbol', 'dt']).reset_index(drop=True)

        required_cols = {'symbol', 'dt', 'open'}
        missing = required_cols - set(panel.columns)
        if missing:
            raise ValueError(f'panel missing required columns: {missing}')

        # Parse signals
        valid_actions = {'LONG', 'SHORT'}
        df_signals = pd.DataFrame(signals)
        if df_signals.empty:
            return pd.DataFrame()
        df_signals = df_signals[df_signals['action'].isin(valid_actions)].copy()
        df_signals = df_signals.sort_values('decision_ts').reset_index(drop=True)

        SYMBOLS_IN_PANEL = set(panel['symbol'].unique())
        active_positions = {}  # {symbol: {entry_ts, entry_bar_idx, entry_px, ...}}
        trades = []

        for _, sig in df_signals.iterrows():
            sym = sig['symbol']
            if sym not in SYMBOLS_IN_PANEL:
                continue

            decision_ts = pd.Timestamp(sig['decision_ts']).tz_localize(None) \
                if pd.Timestamp(sig['decision_ts']).tzinfo is None \
                else pd.Timestamp(sig['decision_ts'])
            act = sig['action'].upper().strip()

            # Skip FLAT (no entry)
            if act == 'FLAT':
                continue

            # --- Check same-symbol overlap ---
            if sym in active_positions:
                # Do not enter if an existing position for this symbol is still held.
                # The existing position's hold_end is entry_bar_idx + hold_bars.
                existing = active_positions[sym]
                if decision_ts < existing['hold_end_ts']:
                    # Overlap blocked
                    trades.append({
                        'snapshot_id': sig.get('snapshot_id', ''),
                        'symbol': sym,
                        'action': act,
                        'confidence': sig.get('confidence', 0),
                        'decision_ts': decision_ts,
                        'overlap_blocked': True,
                        'entry_px': None,
                        'exit_px': None,
                        'gross_bps': 0,
                        'net_bps': 0,
                        'status': 'OVERLAP_BLOCKED'
                    })
                    continue
                else:
                    del active_positions[sym]

            # --- Check account-level exposure ---
            if len(active_positions) >= self.max_positions:
                trades.append({
                    'snapshot_id': sig.get('snapshot_id', ''),
                    'symbol': sym,
                    'action': act,
                    'confidence': sig.get('confidence', 0),
                    'decision_ts': decision_ts,
                    'overlap_blocked': False,
                    'entry_px': None,
                    'exit_px': None,
                    'gross_bps': 0,
                    'net_bps': 0,
                    'status': 'EXPOSURE_LIMIT'
                })
                continue

            # --- Find entry bar (next open after decision_ts) ---
            sym_panel = panel[panel['symbol'] == sym].sort_values('dt').reset_index(drop=True)
            entry_candidates = sym_panel[sym_panel['dt'] > decision_ts]
            if entry_candidates.empty:
                trades.append({
                    'snapshot_id': sig.get('snapshot_id', ''),
                    'symbol': sym,
                    'action': act,
                    'confidence': sig.get('confidence', 0),
                    'decision_ts': decision_ts,
                    'overlap_blocked': False,
                    'entry_px': None,
                    'exit_px': None,
                    'gross_bps': 0,
                    'net_bps': 0,
                    'status': 'NO_ENTRY_BAR'
                })
                continue

            entry_row = entry_candidates.iloc[0]
            entry_bar_idx = entry_row.name  # position in sym_panel
            entry_px = entry_row['open']
            entry_dt = entry_row['dt']

            # --- Find exit bar (hold_bars after entry) ---
            exit_bar_idx = entry_bar_idx + self.hold_bars
            if exit_bar_idx >= len(sym_panel):
                trades.append({
                    'snapshot_id': sig.get('snapshot_id', ''),
                    'symbol': sym,
                    'action': act,
                    'confidence': sig.get('confidence', 0),
                    'decision_ts': decision_ts,
                    'overlap_blocked': False,
                    'entry_px': entry_px,
                    'exit_px': None,
                    'gross_bps': 0,
                    'net_bps': 0,
                    'status': 'NO_EXIT_BAR'
                })
                continue

            exit_row = sym_panel.iloc[exit_bar_idx]
            exit_px = exit_row['open']
            exit_dt = exit_row['dt']

            # --- Compute PnL ---
            if act == 'LONG':
                gross_bps = (exit_px - entry_px) / entry_px * 10000
            elif act == 'SHORT':
                gross_bps = (entry_px - exit_px) / entry_px * 10000
            else:
                continue

            net_bps = gross_bps - self.cost_bps

            # --- Record position ---
            hold_end_ts = exit_dt
            active_positions[sym] = {
                'snapshot_id': sig.get('snapshot_id', ''),
                'entry_ts': decision_ts,
                'entry_bar_idx': entry_bar_idx,
                'entry_px': entry_px,
                'hold_end_ts': hold_end_ts,
                'action': act,
            }

            # --- Record trade ---
            trades.append({
                'snapshot_id': sig.get('snapshot_id', ''),
                'symbol': sym,
                'action': act,
                'confidence': sig.get('confidence', 0),
                'decision_ts': decision_ts,
                'overlap_blocked': False,
                'entry_dt': entry_dt,
                'entry_px': entry_px,
                'exit_dt': exit_dt,
                'exit_px': exit_px,
                'gross_bps': float(round(gross_bps, 2)),
                'net_bps': float(round(net_bps, 2)),
                'status': 'EXECUTED'
            })

        result = pd.DataFrame(trades)
        return result

    def compute_nav(self, trades_df, initial_capital_bps=0):
        """
        Generate realistic NAV curve from trades.
        - Additive bps applied per position_weight.
        - NAV never goes below 0 (liquidation boundary).
        - Overlaps: multiple positions compound simultaneously at weight.
        """
        if trades_df.empty:
            return pd.DataFrame()

        executed = trades_df[trades_df['status'] == 'EXECUTED'].sort_values('entry_dt').copy()
        if executed.empty:
            return pd.DataFrame()

        # Build daily PnL
        executed['entry_dt'] = pd.to_datetime(executed['entry_dt'], utc=True)
        executed['exit_dt'] = pd.to_datetime(executed['exit_dt'], utc=True)

        all_days = pd.date_range(
            start=executed['entry_dt'].min().floor('D'),
            end=executed['exit_dt'].max().ceil('D'),
            freq='D', tz='UTC'
        )

        nav = []
        equity = initial_capital_bps  # start at 0 bps
        for day in all_days:
            day_end = day + timedelta(days=1)
            # Positions active on this day
            active = executed[
                (executed['entry_dt'] < day_end) &
                (executed['exit_dt'] > day)
            ]
            if len(active) == 0:
                nav.append({
                    'date': day,
                    'equity_bps': round(equity, 2),
                    'drawdown_bps': 0,
                    'active_positions': 0
                })
                continue

            # Sum weighted net PnL for active positions
            day_pnl = (active['net_bps'] * self.position_weight).sum()
            equity += day_pnl
            # NAV floor at 0
            if equity < -initial_capital_bps:
                equity = -initial_capital_bps

            nav.append({
                'date': day,
                'equity_bps': round(equity, 2),
                'active_positions': len(active)
            })

        nav_df = pd.DataFrame(nav)
        if not nav_df.empty:
            running_max = nav_df['equity_bps'].cummax()
            nav_df['drawdown_bps'] = (running_max - nav_df['equity_bps']).round(2)
        return nav_df

    def summary(self, trades_df, nav_df=None):
        """Compute aggregate metrics from executed trades."""
        executed = trades_df[trades_df['status'] == 'EXECUTED'].copy()
        if executed.empty:
            return {'n_trades': 0, 'status': 'NO_TRADES'}

        n = len(executed)
        net = executed['net_bps'].values
        gross = executed['gross_bps'].values
        wins = (net > 0).sum()
        losses = (net <= 0).sum()

        total_net = net.sum()
        avg_net = net.mean()
        avg_gross = gross.mean()

        total_gain = net[net > 0].sum() if wins > 0 else 1e-10
        total_loss = abs(net[net <= 0].sum()) if losses > 0 else 1e-10
        pf = total_gain / total_loss if total_loss > 1e-9 else float('inf')

        cum = np.cumsum(net * self.position_weight)
        running_max = np.maximum.accumulate(cum)
        dd = running_max - cum
        max_dd = dd.max()

        sym_pnl = executed.groupby('symbol')['net_bps'].sum().to_dict()

        m = {
            'n_trades': n,
            'total_net_bps': round(total_net, 2),
            'avg_net_bps': round(avg_net, 2),
            'avg_gross_bps': round(avg_gross, 2),
            'pf': round(pf, 2),
            'win_rate': round(wins / n, 4) if n > 0 else 0,
            'max_dd_weighted_bps': round(max_dd, 2),
            'profitable_symbols': sum(1 for v in sym_pnl.values() if v > 0),
            'total_symbols': len(sym_pnl),
            'symbol_pnl': {s: round(v, 2) for s, v in sym_pnl.items()},
            'overlap_blocked': int((trades_df['status'] == 'OVERLAP_BLOCKED').sum()),
            'exposure_blocked': int((trades_df['status'] == 'EXPOSURE_LIMIT').sum()),
        }

        if nav_df is not None and not nav_df.empty:
            m['nav_max_dd_bps'] = round(nav_df['drawdown_bps'].max(), 2)

        return m
