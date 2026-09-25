"""Fetch BRK (Bitcoin Research Kit, bitview.space) on-chain metrics.

BRK has no exchange-labeled reserve data (CryptoQuant's moat). We use cleaner
on-chain proxies: whale-cohort supply, LTH/STH supply, profit share, MVRV.

Output: data/brk_data.json
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path
import requests

API = "https://bitview.space/api"
ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "brk_data.json"

# (metric_key, output_label)
METRICS = [
    ("addrs_over_1k_btc_supply",  "whale_supply"),         # >=1k BTC 鲸鱼供应
    ("addrs_over_10k_btc_supply", "mega_whale_supply"),    # >=10k BTC 巨鲸供应
    ("lth_supply",                "lth_supply"),           # 长期持有者
    ("sth_supply",                "sth_supply"),           # 短期持有者
    ("supply_in_profit_share",    "supply_in_profit_pct"), # 盈利供应比例
    ("mvrv",                      "mvrv"),                 # MVRV 估值
    ("nupl",                      "nupl"),                 # NUPL 净未实现盈亏
    # sopr 已删（2026-07-01）：bitview 该 metric 404 且报告全程 0 引用，白报 error 干扰健康检查
    ("supply",                    "total_supply"),         # BTC 总流通量（用于算 whales%）
]

DAYS = 365
# 鲸鱼占比长时序（PPT 因素 4 图 1 要求 ~5 年 vs BTC 价格双轴，替换 CoinShares Exhibit 35 截图）
# 实测 bitview.space 支持 from=-2000（2026-07-01 验证），必须用 metric 名 `supply`（total_supply 会 404）
WHALES_PCT_LONG_DAYS = 2000


def fetch_metric_series(metric: str, days: int) -> list[float]:
    url = f"{API}/metric/{metric}/day1/data?from=-{days}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    today = date.today()
    dates = [(today - timedelta(days=DAYS - 1 - i)).isoformat() for i in range(DAYS)]

    series: dict[str, list[float]] = {}
    errors: list[str] = []
    for metric, label in METRICS:
        try:
            print(f"BRK {metric} -> {label} ...", file=sys.stderr)
            series[label] = fetch_metric_series(metric, DAYS)
        except Exception as exc:
            errors.append(f"{metric}: {exc}")
            series[label] = []

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
    whales_pct_long = {"dates": [], "values": []}
    try:
        print(f"BRK whales_pct long ({WHALES_PCT_LONG_DAYS}d) ...", file=sys.stderr)
        ws_long = fetch_metric_series("addrs_over_1k_btc_supply", WHALES_PCT_LONG_DAYS)
        ts_long = fetch_metric_series("supply", WHALES_PCT_LONG_DAYS)
        n = min(len(ws_long), len(ts_long))
        long_dates = [(today - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]
        long_values = []
        for i in range(n):
            t, w = ts_long[i], ws_long[i]
            long_values.append(round(w / t * 100, 4) if t and t > 0 else None)
        whales_pct_long = {"dates": long_dates, "values": long_values}
    except Exception as exc:
        errors.append(f"whales_pct_long: {exc}")

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
    }
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"OK: wrote {OUT} ({len(series)} metrics, {len(errors)} errors)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
