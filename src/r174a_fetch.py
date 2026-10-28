# -*- coding: utf-8 -*-
"""
R174A-HL-SPOT-PERP-CARRY — data fetcher (Hyperliquid public API, no key).

Fetches, deterministically and idempotently, into data/:
  - meta / spotMeta / perp assetCtxs / spot assetCtxs
  - candidate resolution: USDC-quoted spot pairs whose base token has a same-name perp
  - 1d candles for spot pair AND perp (used by r174a_universe.py price cross-validation)
  - fundingHistory (paginated, 500/page) for each candidate
  - 1h candles (API retains ~5000 most recent bars) for each candidate

Design notes (from 2026-08-16 memo, P0.1):
  * environment has HTTP(S)_PROXY pointing at a dead <local-proxy> -> trust_env=False + proxies={}
  * @N is a spot slot id, NOT a placeholder; candleSnapshot('@N') returns SLOT history that can be
    polluted by a previously listed token. We therefore fetch it anyway and let the universe script
    reject it via price cross-validation. We never assume @N is trustworthy.
  * funding timestamps carry a millisecond offset -> keep raw; floor to hour downstream.
  * fundingHistory returns max 500 rows per call, ascending -> paginate on last time + 1ms.

Usage:
  python r174a_fetch.py            # fetch only what is missing (idempotent)
  python r174a_fetch.py --force    # refetch everything
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.hyperliquid.xyz/info"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
RAW = os.path.join(DATA, "raw")
CANDLES = os.path.join(DATA, "candles")
FUNDING = os.path.join(DATA, "funding")

MIN_INTERVAL = 0.55          # seconds between HTTP calls (429 avoidance; HL info endpoint is bursty)
MAX_RETRIES = 6
UTC = timezone.utc

# ---------------------------------------------------------------- http layer
_SESSION = requests.Session()
_SESSION.trust_env = False   # ignore dead proxy env vars
_SESSION.proxies = {"http": None, "https": None}
_last_call = [0.0]
_stats = {"calls": 0, "retries": 0, "429": 0}


def _sleep_to_rate_limit():
    wait = MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def post_info(body, retries=MAX_RETRIES):
    """POST to /info with rate limit + retry (429 friendly)."""
    last_err = None
    for attempt in range(retries):
        _sleep_to_rate_limit()
        try:
            _stats["calls"] += 1
            r = _SESSION.post(BASE_URL, json=body, timeout=45)
            if r.status_code == 429:
                _stats["429"] += 1
                backoff = 4.0 * (attempt + 1)
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        backoff = max(backoff, float(ra))
                    except ValueError:
                        pass
                print(f"  [429] {body.get('type')} -> backoff {backoff:.1f}s", file=sys.stderr)
                time.sleep(backoff)
                last_err = "429"
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # network / 5xx / json
            last_err = repr(e)
            _stats["retries"] += 1
            print(f"  [retry {attempt+1}/{retries}] {body.get('type')} {last_err[:160]}", file=sys.stderr)
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"API failed after {retries} tries: {body} last_err={last_err}")


# ---------------------------------------------------------------- io helpers
def ensure_dirs():
    for d in (DATA, RAW, CANDLES, FUNDING):
        os.makedirs(d, exist_ok=True)


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def exists(path, force):
    return (not force) and os.path.exists(path) and os.path.getsize(path) > 2


def sanitize(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_")


# ---------------------------------------------------------------- fetchers
def fetch_static(force):
    """meta / spotMeta / asset ctxs snapshots."""
    jobs = [
        ("meta", {"type": "meta"}),
        ("spot_meta", {"type": "spotMeta"}),
        ("perp_asset_ctxs", {"type": "metaAndAssetCtxs"}),
        ("spot_asset_ctxs", {"type": "spotMetaAndAssetCtxs"}),
    ]
    out = {}
    for name, body in jobs:
        path = os.path.join(RAW, f"{name}.json")
        if exists(path, force):
            out[name] = load_json(path)
            print(f"[cache] {name}")
        else:
            out[name] = post_info(body)
            save_json(path, out[name])
            print(f"[fetch] {name}")
    return out


def resolve_candidates(meta, spot_meta):
    """USDC-quoted spot pairs whose base token has a same-name perp. Deterministic rule (SPEC 1.1)."""
    tokens = {t["index"]: t for t in spot_meta["tokens"]}
    perp_names = {u["name"] for u in meta["universe"]}
    cands, skipped_nonusdc = [], []
    for p in spot_meta["universe"]:
        base_idx, quote_idx = p["tokens"][0], p["tokens"][1]
        tok = tokens.get(base_idx)
        if tok is None:
            continue
        name = tok["name"]
        if name not in perp_names:
            continue
        row = {
            "perp": name,
            "spot_pair": p["name"],
            "pair_index": p.get("index"),
            "token_index": base_idx,
            "is_canonical": bool(p.get("isCanonical", False)),
            "quote_token_index": quote_idx,
        }
        if quote_idx != 0:
            skipped_nonusdc.append(row)
        else:
            cands.append(row)
    # stable order
    cands.sort(key=lambda r: r["perp"])
    return cands, skipped_nonusdc


def fetch_candles(coin, interval, start_ms, end_ms, path, force):
    if exists(path, force):
        return load_json(path), "cache"
    body = {"type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval, "startTime": int(start_ms), "endTime": int(end_ms)}}
    data = post_info(body)
    # dedupe by open time, ascending
    if data:
        data = sorted({int(c["t"]): c for c in data}.values(), key=lambda c: int(c["t"]))
    save_json(path, data)
    return data, "fetch"


def fetch_funding(coin, path, force):
    """Paginated fundingHistory (500 rows/call, ascending)."""
    if exists(path, force):
        return load_json(path), "cache"
    rows, start, pages = [], 0, 0
    while True:
        data = post_info({"type": "fundingHistory", "coin": coin, "startTime": int(start)})
        pages += 1
        if not data:
            break
        rows.extend(data)
        if len(data) < 500:
            break
        start = int(data[-1]["time"]) + 1
        if pages > 400:
            raise RuntimeError(f"funding pagination runaway for {coin}")
    # dedupe on time
    if rows:
        rows = sorted({int(r["time"]): r for r in rows}.values(), key=lambda r: int(r["time"]))
    save_json(path, rows)
    return rows, f"fetch({pages} pages)"


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="refetch even if cached")
    ap.add_argument("--skip-hourly", action="store_true", help="skip 1h candles")
    args = ap.parse_args()

    ensure_dirs()
    t0 = time.time()
    static = fetch_static(args.force)
    meta, spot_meta = static["meta"], static["spot_meta"]

    cands, skipped_nonusdc = resolve_candidates(meta, spot_meta)
    print(f"[candidates] USDC-quoted same-name spot/perp pairs: {len(cands)}")
    for c in cands:
        print(f"   {c['perp']:10s} spot_pair={c['spot_pair']:12s} canonical={c['is_canonical']}")
    print(f"[candidates] skipped non-USDC-quoted same-name pairs: {len(skipped_nonusdc)}")
    for c in skipped_nonusdc:
        print(f"   {c['perp']:10s} spot_pair={c['spot_pair']:12s} quote_tok={c['quote_token_index']}")
    save_json(os.path.join(DATA, "candidates.json"),
              {"generated_at": datetime.now(UTC).isoformat(),
               "candidates": cands, "skipped_non_usdc": skipped_nonusdc})

    now_ms = int(time.time() * 1000)
    # 1d history: startTime=0 gives full available history (well under 5000 days)
    manifest = {"generated_at": datetime.now(UTC).isoformat(), "rows": {}, "ranges": {},
                "request_stats": None, "candidates": cands, "skipped_non_usdc": skipped_nonusdc}
    print("\n[fetch] 1d candles (spot pair + perp) for all candidates")
    for c in cands:
        sym, pair = c["perp"], c["spot_pair"]
        for leg, coin in (("spot", pair), ("perp", sym)):
            path = os.path.join(CANDLES, f"1d_{leg}_{sanitize(sym)}.json")
            data, how = fetch_candles(coin, "1d", 0, now_ms, path, args.force)
            key = f"1d_{leg}_{sym}"
            manifest["rows"][key] = len(data)
            manifest["ranges"][key] = [data[0]["t"], data[-1]["t"]] if data else None
            print(f"   {key:24s} n={len(data):5d} "
                  f"{_iso(data[0]['t']) if data else '-'} -> {_iso(data[-1]['t']) if data else '-'} [{how}]")

    print("\n[fetch] fundingHistory for all candidates")
    for c in cands:
        sym = c["perp"]
        path = os.path.join(FUNDING, f"{sanitize(sym)}.json")
        data, how = fetch_funding(sym, path, args.force)
        key = f"funding_{sym}"
        manifest["rows"][key] = len(data)
        manifest["ranges"][key] = [data[0]["time"], data[-1]["time"]] if data else None
        print(f"   {key:24s} n={len(data):6d} "
              f"{_iso(data[0]['time']) if data else '-'} -> {_iso(data[-1]['time']) if data else '-'} [{how}]")

    if not args.skip_hourly:
        print("\n[fetch] 1h candles (API retains ~5000 most recent bars only)")
        # probe retention once (PURR) then use now-6000h window for the rest
        hr_start = now_ms - 6000 * 3600 * 1000
        probe_path = os.path.join(CANDLES, "_retention_probe_1h.json")
        probe, _ = fetch_candles("PURR", "1h", now_ms - 400 * 86400 * 1000, now_ms, probe_path, args.force)
        manifest["retention_probe_1h"] = {
            "requested_from_ms": now_ms - 400 * 86400 * 1000,
            "n": len(probe), "first_t": probe[0]["t"] if probe else None,
            "last_t": probe[-1]["t"] if probe else None,
            "interpretation": "if first_t > requested_from_ms the API truncates history",
        }
        print(f"   retention probe: n={len(probe)} "
              f"{_iso(probe[0]['t']) if probe else '-'} -> {_iso(probe[-1]['t']) if probe else '-'}")
        for c in cands:
            sym, pair = c["perp"], c["spot_pair"]
            for leg, coin in (("spot", pair), ("perp", sym)):
                path = os.path.join(CANDLES, f"1h_{leg}_{sanitize(sym)}.json")
                data, how = fetch_candles(coin, "1h", hr_start, now_ms, path, args.force)
                key = f"1h_{leg}_{sym}"
                manifest["rows"][key] = len(data)
                manifest["ranges"][key] = [data[0]["t"], data[-1]["t"]] if data else None
                print(f"   {key:24s} n={len(data):5d} "
                      f"{_iso(data[0]['t']) if data else '-'} -> {_iso(data[-1]['t']) if data else '-'} [{how}]")

    manifest["request_stats"] = dict(_stats)
    manifest["elapsed_s"] = round(time.time() - t0, 1)
    save_json(os.path.join(DATA, "manifest.json"), manifest)
    print(f"\n[done] {time.time()-t0:.1f}s calls={_stats['calls']} 429s={_stats['429']} "
          f"-> {os.path.join(DATA, 'manifest.json')}")


def _iso(ms):
    return datetime.fromtimestamp(int(ms) / 1000, UTC).strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    main()

