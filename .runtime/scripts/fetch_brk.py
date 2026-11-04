"""Fetch BRK (Bitcoin Research Kit, bitview.space) on-chain metrics.

BRK has no exchange-labeled reserve data (CryptoQuant's moat). We use cleaner
on-chain proxies: whale-cohort supply, LTH/STH supply, profit share, MVRV.

Output: data/brk_data.json
"""
import json
import hashlib
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import requests

API = "https://bitview.space/api"
ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "brk_data.json"

# (source series, output label). Address bands are disjoint; summing them
# preserves the previous >=1k / >=10k BTC address-cohort definitions.
METRICS = [
    (("balance_1k_to_10k_btc_supply", "balance_10k_to_100k_btc_supply", "balance_over_100k_btc_supply"), "whale_supply"),
    (("balance_10k_to_100k_btc_supply", "balance_over_100k_btc_supply"), "mega_whale_supply"),
    (("lth_supply",), "lth_supply"),
    (("sth_supply",), "sth_supply"),
    (("supply_in_profit_share",), "supply_in_profit_pct"),
    (("mvrv",), "mvrv"),
    (("nupl",), "nupl"),
    # sopr 已删（2026-07-01）：bitview 该 metric 404 且报告全程 0 引用，白报 error 干扰健康检查
    (("circulating_supply",), "total_supply"),
]

DAYS = 365
# 鲸鱼占比长时序（PPT 因素 4 图 1 要求 ~5 年 vs BTC 价格双轴，替换 CoinShares Exhibit 35 截图）
# New API uses start, and returns the server's actual dates in the same batch.
WHALES_PCT_LONG_DAYS = 2000
SOURCE_SERIES = list(dict.fromkeys(name for names, _ in METRICS for name in names))


def parse_snapshot(raw: list, today: date | None = None) -> tuple:
    today = today or datetime.now(timezone.utc).date()
    if len(raw) != 1 + len(SOURCE_SERIES):
        raise ValueError("BRK bulk response has unexpected component count")
    if len({(x['start'], x['end'], x['index']) for x in raw}) != 1:
        raise ValueError("BRK bulk components do not share an index range")
    if raw[0]['index'] != 'day1' or raw[0].get('type') != 'Date':
        raise ValueError("BRK dates are not daily calendar dates")
    n = raw[0]['end'] - raw[0]['start']
    if any(len(x['data']) != n for x in raw):
        raise ValueError("BRK bulk component length mismatch")
    dates = [date.fromisoformat(d) for d in raw[0]['data']]
    if any(b - a != timedelta(days=1) for a, b in zip(dates, dates[1:])):
        raise ValueError("BRK daily calendar has gaps or duplicates")
    keep = [i for i, day in enumerate(dates) if day < today][-WHALES_PCT_LONG_DAYS:]
    if len(keep) < WHALES_PCT_LONG_DAYS or (today - dates[keep[-1]]).days > 4:
        raise ValueError("BRK complete daily history is too short or stale")
    values = {}
    for name, block in zip(SOURCE_SERIES, raw[1:]):
        expected_type = ('Percent' if name == 'supply_in_profit_share' else
                         'Multiple' if name == 'mvrv' else
                         'Ratio' if name == 'nupl' else 'Bitcoin')
        if block.get('type') != expected_type:
            raise ValueError(f"BRK unit changed for {name}: {block.get('type')}")
        selected = [block['data'][i] for i in keep]
        if any(v is None or not math.isfinite(v) for v in selected):
            raise ValueError(f"BRK missing or invalid values in {name}")
        values[name] = selected
    series = {label: [sum(parts) for parts in zip(*(values[name] for name in names))]
              for names, label in METRICS}
    if any(not 0 <= mega <= whale <= total for mega, whale, total in
           zip(series['mega_whale_supply'], series['whale_supply'], series['total_supply'])):
        raise ValueError("BRK address cohort supplies exceed total supply")
    return [dates[i].isoformat() for i in keep], series


def fetch_snapshot() -> tuple:
    r = requests.get(f"{API}/series/bulk", params={
        'series': ','.join(['date'] + SOURCE_SERIES), 'index': 'day1',
        'start': -(WHALES_PCT_LONG_DAYS + 1),
    }, timeout=45)
    r.raise_for_status()
    raw = r.json()
    dates, series = parse_snapshot(raw)
    return dates, series, {
        'url': r.url, 'versions': [x.get('version') for x in raw],
        'raw_sha256': hashlib.sha256(r.content).hexdigest(),
        'day_policy': 'completed UTC days; server date series',
        'source_series': {label: list(names) for names, label in METRICS},
    }


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).date()
    errors: list[str] = []
    try:
        long_dates, long_series, provenance = fetch_snapshot()
    except Exception as exc:
        print(f"FAIL BRK: {exc}; previous file retained", file=sys.stderr)
        return 1
    dates = long_dates[-DAYS:]
    series = {label: values[-DAYS:] for label, values in long_series.items()}

    # Whale Accumulation Score — synthesized to replace Glassnode's paid metric.
    # Method: 30d delta in addrs_over_1k_btc_supply, z-scored over the 365d window.
    # Glassnode's score is 0-1 (>0.7 accumulation, <0.3 distribution).
    # We map: z >= +1.5 → 0.9 (strong accumulation), z <= -1.5 → 0.1 (strong distribution),
    #         interp linearly between -1.5 and +1.5 → 0.2..0.8.
    ws = series.get("whale_supply") or []
    accum_score = {
        "whale_7d_change": None,
        "whale_30d_change": None,
        "whale_current": None,
        "whale_accumulation_score": None,
        "whale_accumulation_z": None,
        "whale_accumulation_label": None,
    }
    if len(ws) >= 31:
        accum_score["whale_current"] = ws[-1]
        accum_score["whale_7d_change"] = ws[-1] - ws[-8]
        accum_score["whale_30d_change"] = ws[-1] - ws[-31]

        # 30d rolling delta series for z-score
        deltas = [ws[i] - ws[i - 30] for i in range(30, len(ws))]
        if len(deltas) >= 30:
            import statistics
            mu = statistics.mean(deltas)
            sd = statistics.pstdev(deltas) or 1.0
            current_delta = deltas[-1]
            z = (current_delta - mu) / sd
            # Map z to 0..1 Glassnode-style score
            score = max(0.0, min(1.0, 0.5 + z / 3.0))  # z=0 → 0.5, z=±1.5 → 0.0/1.0
            if score >= 0.7:
                label = "积累 (Accumulation)"
            elif score <= 0.3:
                label = "分发 (Distribution)"
            else:
                label = "中性 (Neutral)"
            accum_score["whale_accumulation_score"] = round(score, 3)
            accum_score["whale_accumulation_z"] = round(z, 2)
            accum_score["whale_accumulation_label"] = label

    # latest snapshot summary for dashboard row
    latest = {label: (vals[-1] if vals else None) for label, vals in series.items()}

    # whales ownership % series（PPT 因素 4 图 1）—— 自合成 whale_supply / total_supply * 100
    whale_supply_series = series.get("whale_supply") or []
    total_supply_series = series.get("total_supply") or []
    whales_pct_series = []
    if whale_supply_series and total_supply_series:
        n = min(len(whale_supply_series), len(total_supply_series))
        for i in range(n):
            ts = total_supply_series[i]
            ws = whale_supply_series[i]
            if ts and ts > 0:
                whales_pct_series.append(round(ws / ts * 100, 4))
            else:
                whales_pct_series.append(None)

    whales_pct_current = whales_pct_series[-1] if whales_pct_series else None
    whales_pct_30d_change = None
    if len(whales_pct_series) > 30 and whales_pct_series[-1] is not None and whales_pct_series[-31] is not None:
        whales_pct_30d_change = round(whales_pct_series[-1] - whales_pct_series[-31], 4)

    # whales% 长时序（~5.5 年，PPT 因素 4 图 1 用，替换 CoinShares Exhibit 35 截图）
    whales_pct_long = {'dates': long_dates, 'values': [
        round(w / t * 100, 4) for w, t in
        zip(long_series['whale_supply'], long_series['total_supply'])
    ]}

    output = {
        "fetched_at": today.isoformat(),
        "days": DAYS,
        "dates": dates,
        "series": series,
        "latest": latest,
        "accumulation": accum_score,
        "whales_pct": {
            "series": whales_pct_series,
            "current": whales_pct_current,
            "change_30d": whales_pct_30d_change,
            "note": "whale_supply (>=1k BTC) / total_supply * 100",
        },
        "whales_pct_long": whales_pct_long,
        "errors": errors,
        "source": "bitview.space (Bitcoin Research Kit)",
        "provenance": provenance,
    }
    temporary = OUT.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    temporary.replace(OUT)
    print(f"OK: wrote {OUT} ({len(series)} metrics, {len(errors)} errors)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
