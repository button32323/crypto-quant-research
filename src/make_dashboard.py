#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 paper trading 可视化仪表盘（单文件 HTML，内联 SVG，不依赖外部库）。

用法：
    python make_dashboard.py
输出：
    DASHBOARD.html
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "paper_log.csv"
STATE = HERE / "state.json"
VALIDATION = HERE / "validation.json"
READINESS = HERE / "readiness.json"
FUNDING_REC = HERE / "funding_reconcile.json"
NAV_REC = HERE / "nav_reconcile.json"
FUNDING_ENV = HERE / "funding_environment.json"
OUT = HERE / "DASHBOARD.html"


def read_csv_rows() -> list[dict]:
    if not LOG.exists():
        return []
    with LOG.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def svg_chart(rows: list[dict], width: int = 920, height: int = 280) -> str:
    navs = [float(r["nav_usd"]) for r in rows if r.get("nav_usd")]
    if len(navs) < 2:
        return '<p class="muted">数据不足，暂无净值曲线。</p>'
    pad_l, pad_r, pad_t, pad_b = 64, 20, 20, 34
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    lo, hi = min(navs), max(navs)
    if hi - lo < 1e-9:
        hi = lo + 1
    base = float(rows[0]["nav_usd"])

    def x(i: int) -> float:
        return pad_l + w * i / (len(navs) - 1)

    def y(v: float) -> float:
        return pad_t + h * (1 - (v - lo) / (hi - lo))

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(navs))
    y_base = y(base)
    y_lo = y(lo)
    y_hi = y(hi)
    t0 = rows[0]["ts"][:16].replace("T", " ")
    t1 = rows[-1]["ts"][:16].replace("T", " ")
    color = "#2e7d32" if navs[-1] >= base else "#c62828"
    return f"""<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="NAV 曲线">
  <line x1="{pad_l}" y1="{y_hi:.1f}" x2="{width-pad_r}" y2="{y_hi:.1f}" stroke="#e0e0e0"/>
  <line x1="{pad_l}" y1="{y_lo:.1f}" x2="{width-pad_r}" y2="{y_lo:.1f}" stroke="#e0e0e0"/>
  <text x="{pad_l-8}" y="{y_hi+4:.1f}" text-anchor="end" class="axis">{hi:,.2f}</text>
  <text x="{pad_l-8}" y="{y_lo+4:.1f}" text-anchor="end" class="axis">{lo:,.2f}</text>
  <line x1="{pad_l}" y1="{y_base:.1f}" x2="{width-pad_r}" y2="{y_base:.1f}" stroke="#9e9e9e" stroke-dasharray="5 4"/>
  <text x="{width-pad_r}" y="{y_base-6:.1f}" text-anchor="end" class="axis">建仓净值 {base:,.2f}</text>
  <polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.2"/>
  <text x="{pad_l}" y="{height-10}" class="axis">{t0} (UTC)</text>
  <text x="{width-pad_r}" y="{height-10}" text-anchor="end" class="axis">{t1} (UTC)</text>
</svg>"""


def main() -> int:
    rows = read_csv_rows()
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    val = json.loads(VALIDATION.read_text(encoding="utf-8")) if VALIDATION.exists() else {}
    readiness = json.loads(READINESS.read_text(encoding="utf-8")) if READINESS.exists() else {}
    funding_rec = json.loads(FUNDING_REC.read_text(encoding="utf-8")) if FUNDING_REC.exists() else {}
    nav_rec = json.loads(NAV_REC.read_text(encoding="utf-8")) if NAV_REC.exists() else {}
    fenv = json.loads(FUNDING_ENV.read_text(encoding="utf-8")) if FUNDING_ENV.exists() else {}

    latest = rows[-1] if rows else {}
    nav = float(latest.get("nav_usd") or state.get("capital_usd", 1000))
    capital = float(state.get("capital_usd", 1000))
    ret_pct = (nav / capital - 1) * 100
    days = float(val.get("days_elapsed") or 0)
    pct_days = min(days / 30 * 100, 100)

    def card(label: str, value: str, cls: str = "") -> str:
        return f'<div class="card"><div class="label">{label}</div><div class="value {cls}">{value}</div></div>'

    cards = "".join([
        card("虚拟净值", f"${nav:,.2f}", "pos" if ret_pct >= 0 else "neg"),
        card("累计收益", f"{ret_pct:+.3f}%", "pos" if ret_pct >= 0 else "neg"),
        card("资金费累计", f"${float(latest.get('funding_accrued_usd') or 0):+.4f}"),
        card("Basis 盈亏", f"${float(latest.get('basis_pnl_usd') or 0):+.4f}"),
        card("资金费结算", f"{val.get('funding_settlements', 0)} 次 / 正占比 {float(val.get('funding_positive_share') or 0):.1%}"),
        card("费率预期", f"${float(state.get('expected_funding_per_hour') or 0):+.4f}/h"),
        card("最大回撤", f"{float(val.get('max_drawdown_pct') or 0):.3f}%"),
        card("验证状态", str(val.get("status", "—")),
             "pos" if val.get("status") == "PASS" else ("neg" if val.get("status") == "FAIL" else "")),
        card("就绪状态", str(readiness.get("overall", "—"))),
        card("对账", f"资金费 {funding_rec.get('overall', '—')} / NAV {nav_rec.get('overall', '—')}"),
        card("环境分位", f"{fenv.get('current_percentile_30d', '—')}% / 年化 {fenv.get('current_annualized_pct', '—')}%"),
    ])

    readiness_rows = "".join(
        f"<tr><td>{i['name']}</td><td class='{'ok' if i['ok'] else 'bad'}'>{'✅' if i['ok'] else '❌'}</td>"
        f"<td>{i['detail']}</td></tr>"
        for i in readiness.get("items", []))

    recent = rows[-12:][::-1]
    recent_rows = "".join(
        f"<tr><td>{r.get('ts','')[:19].replace('T',' ')}</td><td>${float(r.get('nav_usd') or 0):,.4f}</td>"
        f"<td>{float(r.get('ret_pct') or 0):+.4f}%</td><td>${float(r.get('funding_accrued_usd') or 0):+.6f}</td>"
        f"<td>${float(r.get('basis_pnl_usd') or 0):+.4f}</td></tr>"
        for r in recent)

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Paper Trading 仪表盘 — HL Carry</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 24px auto; max-width: 980px; color: #212121; background: #fafafa; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 18px; }}
  .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 18px; }}
  .card {{ background: #fff; border: 1px solid #e8e8e8; border-radius: 8px; padding: 10px 12px; }}
  .label {{ color: #757575; font-size: 12px; }}
  .value {{ font-size: 18px; font-weight: 600; margin-top: 4px; }}
  .pos {{ color: #2e7d32; }} .neg {{ color: #c62828; }}
  .chart {{ width: 100%; height: auto; background: #fff; border: 1px solid #e8e8e8; border-radius: 8px; }}
  .axis {{ font-size: 11px; fill: #757575; }}
  .bar {{ background: #e0e0e0; border-radius: 6px; height: 12px; overflow: hidden; margin: 6px 0 20px; }}
  .bar > div {{ height: 100%; background: #1976d2; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e8e8e8; border-radius: 8px; overflow: hidden; margin-bottom: 20px; }}
  th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid #f0f0f0; font-size: 13px; }}
  th {{ background: #f5f5f5; }}
  .ok {{ color: #2e7d32; }} .bad {{ color: #c62828; }}
  .muted {{ color: #9e9e9e; }}
  h2 {{ font-size: 16px; margin: 18px 0 8px; }}
</style></head><body>
<h1>Paper Trading 仪表盘 — HL 现货-永续 Carry（PURR + HYPE）</h1>
<div class="sub">虚拟资金 ${capital:,.0f} · 数据更新 {datetime.now().strftime('%Y-%m-%d %H:%M')} · 30 天验证进度 {days:.2f}/30 天</div>
<div class="cards">{cards}</div>
<div class="bar"><div style="width:{pct_days:.1f}%"></div></div>
{svg_chart(rows)}
<h2>最近快照</h2>
<table><thead><tr><th>时间 (UTC)</th><th>净值</th><th>收益率</th><th>资金费累计</th><th>Basis 盈亏</th></tr></thead>
<tbody>{recent_rows or '<tr><td colspan="5" class="muted">暂无数据</td></tr>'}</tbody></table>
<h2>实盘就绪检查</h2>
<table><thead><tr><th>检查项</th><th>状态</th><th>详情</th></tr></thead>
<tbody>{readiness_rows or '<tr><td colspan="3" class="muted">暂无数据</td></tr>'}</tbody></table>
<p class="muted">本页面由 make_dashboard.py 生成；全部为虚拟资金，不涉及真实下单。</p>
</body></html>"""
    OUT.write_text(html, encoding="utf-8")
    print(f"已生成: {OUT}（{len(html)/1024:.1f} KB，{len(rows)} 条快照）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



