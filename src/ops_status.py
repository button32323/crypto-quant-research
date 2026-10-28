#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无人值守状态页（OPS.md）：进程、心跳、对账、告警统计一页可见。

用法：python ops_status.py
输出：OPS.md / ops_status.json
"""
from __future__ import annotations

import json
import shutil
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_JSON = HERE / "ops_status.json"
OUT_MD = HERE / "OPS.md"


def ps_json(cmd: str) -> list[dict]:
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        txt = (proc.stdout or "").strip()
        if not txt:
            return []
        data = json.loads(txt)
        return data if isinstance(data, list) else [data]
    except Exception:  # noqa: BLE001
        return []


def load(name: str) -> dict:
    p = HERE / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def count_alerts(name: str) -> int:
    p = HERE / name
    if not p.exists():
        return 0
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        return sum(1 for ln in txt.splitlines() if "ALERT" in ln or "FAILED" in ln)
    except Exception:  # noqa: BLE001
        return 0


def main() -> int:
    procs = ps_json(
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
        "Where-Object { $_.CommandLine -match '-File\\s+\"?[^\"]*[\\\\/](paper_runner|supervisor)\\.ps1\"?\\s*$' } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    roles = []
    for p in procs:
        c = str(p.get("CommandLine") or "")
        role = "supervisor" if "supervisor.ps1" in c else ("runner" if "paper_runner.ps1" in c else "other")
        if role != "other":
            roles.append({"role": role, "pid": p.get("ProcessId")})
    hb = ""
    hb_path = HERE / "heartbeat.txt"
    if hb_path.exists():
        hb = hb_path.read_text(encoding="utf-8", errors="ignore").strip().lstrip("\ufeff").strip()
    hb_age = None
    if hb:
        try:
            hb_age = (datetime.now() - datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
        except Exception:  # noqa: BLE001
            hb_age = None

    fr = load("funding_reconcile.json")
    nr = load("nav_reconcile.json")
    rd = load("readiness.json")
    val = load("validation.json")
    st = load("state.json")
    expected_hourly = st.get("expected_funding_per_hour")
    logs = {name: count_alerts(name) for name in ("watchdog.log", "supervisor.log", "runner.log")}
    # 近 24h 资金费均值（比瞬时费率更稳的预期）
    trailing = None
    try:
        sys.path.insert(0, str(HERE))
        import paper_hl_carry as ph  # noqa: E402
        now_ms = int(datetime.now().timestamp() * 1000)
        start = now_ms - 24 * 3600 * 1000
        total = 0.0
        for name, pos in (st.get("positions") or {}).items():
            rows_f = ph.fetch_funding_since(pos["perp"], start)
            if rows_f:
                total += sum(float(r["fundingRate"]) for r in rows_f) / len(rows_f) * pos["notional_usd"]
        trailing = round(total, 6)
    except Exception:  # noqa: BLE001
        trailing = None

    now_utc = datetime.now(timezone.utc)
    next_hour = (now_utc + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    minutes_to_settlement = round((next_hour - now_utc).total_seconds() / 60, 1)
    disk = shutil.disk_usage(str(HERE))
    paper_mb = round(sum(p.stat().st_size for p in HERE.glob("*") if p.is_file()) / 1024 / 1024, 2)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "processes": roles,
        "heartbeat": hb,
        "heartbeat_age_minutes": round(hb_age, 1) if hb_age is not None else None,
        "funding_reconcile": fr.get("overall"),
        "nav_reconcile": nr.get("overall"),
        "readiness": rd.get("overall"),
        "paper_status": val.get("status"),
        "expected_funding_per_hour_usd": expected_hourly,
        "next_settlement_estimate_usd": (round(float(expected_hourly), 6) if expected_hourly is not None else None),
        "alert_counts": logs,
        "disk_free_gb": round(disk.free / 1024**3, 1),
        "paper_dir_mb": paper_mb,
        "trailing_24h_funding_per_hour_usd": trailing,
        "next_settlement_utc": next_hour.isoformat(timespec="minutes"),
        "minutes_to_next_settlement": minutes_to_settlement,
    }
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# 无人值守状态（OPS）", "",
        f"- 生成时间：{result['generated_at']}", "",
        "| 项 | 状态 |", "|---|---|",
        "| 进程 | " + (", ".join(f"{r['role']}(pid {r['pid']})" for r in roles) or "**未发现**") + " |",
        f"| 心跳 | {hb or '—'}（{f'{hb_age:.1f} 分钟前' if hb_age is not None else 'n/a'}） |",
        f"| 资金费对账 | {fr.get('overall', '—')} |",
        f"| NAV 对账 | {nr.get('overall', '—')} |",
        f"| 实盘就绪 | {rd.get('overall', '—')} |",
        f"| paper 状态 | {val.get('status', '—')} |",
        f"| 下次结算预计（按当前费率 1 小时） | ${(f'{float(expected_hourly):+.6f}' if expected_hourly is not None else '—')} |",
        "| 告警计数 | " + ", ".join(f"{k}={v}" for k, v in logs.items()) + " |",
        f"| 资源 | paper 目录 {paper_mb} MB ｜ C 盘剩余 {round(disk.free/1024**3,1)} GB |",
        f"| 距下次结算（UTC 整点） | {minutes_to_settlement:.1f} 分钟（{next_hour.strftime('%H:%M')} UTC） |",
        f"| 近 24h 平均资金费 | {(f'{float(trailing):+.6f} $/h（年化 {float(trailing)*24*365/float(st.get('capital_usd') or 1000)*100:+.1f}%）' if trailing is not None else '—')} |",
        "",
        "> 进程由 WMI 启动，不随开机自启；重启电脑后运行 `start_paper_runner.ps1`。",
        "",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n已生成: {OUT_JSON}\n已生成: {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())








