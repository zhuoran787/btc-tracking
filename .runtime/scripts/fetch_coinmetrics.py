"""Fetch Exchange Reserve (SplyExNtv) from Coin Metrics Community REST API.

数据源（2026-07-02 重构 v2）：
- **主源：Coin Metrics Community REST API**（免费无 key，单页拉全 15 年历史）
    https://community-api.coinmetrics.io/v4/timeseries/asset-metrics
  实测：page_size=10000 一次返回 5548 行（2011-04-24 → today，0 滞后）。
- **Fallback：github.com/coinmetrics/data CSV**（旧主源）。
  该 repo 的 CoinMetrics Bot 2026-05-24 起停更（TODO #17 事故），保留作 API 失败时的降级。

口径：Exchange Reserve（与 CryptoQuant 一致）。

Scope（按 2026-05-18 用户任务限定）：
  ⛔ 只拉 time + SplyExNtv；其他字段（HashRate / AdrActCnt / CapMVRVCur / Flow*）一律不顺手纳入
  ⛔ 不替换 BRK 的 whale_supply / lth_supply / mvrv 等指标

Output: data/coinmetrics_data.json（schema 与 v1 完全兼容）
"""
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
CACHE_DIR = ROOT / "cache" / "cm-data"
CSV_FILE = CACHE_DIR / "csv" / "btc.csv"
OUT = ROOT / "data" / "coinmetrics_data.json"

API_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
REPO_URL = "https://github.com/coinmetrics/data.git"


def fetch_series_api() -> list[dict]:
    """主源：Community REST API 一次拉全量 daily SplyExNtv。"""
    r = requests.get(API_URL, params={
        "assets": "btc", "metrics": "SplyExNtv", "frequency": "1d",
        "start_time": "2009-01-01", "page_size": 10000,
    }, timeout=90)
    r.raise_for_status()
    rows = r.json().get("data", [])
    if len(rows) < 4000:
        raise ValueError(f"API returned too few rows ({len(rows)})")
    series = []
    for row in rows:
        v = row.get("SplyExNtv")
        series.append({
            "date": row["time"][:10],
            "value": float(v) if v is not None else None,
        })
    series.sort(key=lambda x: x["date"])
    return series


def fetch_series_csv() -> list[dict]:
    """Fallback：GitHub CSV（上游 2026-05-24 起停更，数据可能滞后）。"""
    if not CACHE_DIR.exists():
        CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
        print(f"Cloning {REPO_URL} → {CACHE_DIR} (--depth 1, ~30MB) ...", file=sys.stderr)
        subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(CACHE_DIR)], check=True)
    else:
        subprocess.run(["git", "-C", str(CACHE_DIR), "pull", "--ff-only"], check=True)
    if not CSV_FILE.exists():
        raise FileNotFoundError(f"CSV not found at {CSV_FILE}")
    import pandas as pd
    df = pd.read_csv(CSV_FILE, usecols=["time", "SplyExNtv"]).sort_values("time")
    series = []
    for _, row in df.iterrows():
        v = row["SplyExNtv"]
        val = None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)
        series.append({"date": str(row["time"])[:10], "value": val})
    return series


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    source = "community-api.coinmetrics.io/v4 (REST, free tier)"
    try:
        try:
            series = fetch_series_api()
        except Exception as api_exc:
            print(f"WARN coinmetrics REST API failed ({api_exc}), fallback to GitHub CSV "
                  f"(⚠️ 上游 2026-05-24 起停更，数据可能滞后)", file=sys.stderr)
            series = fetch_series_csv()
            source = "github.com/coinmetrics/data CSV (fallback; upstream stalled since 2026-05-24)"

        total_rows = len(series)

        # latest: 最后一个非 null 点
        latest = next((r for r in reversed(series) if r["value"] is not None), None)

        # 30d delta
        delta_30d = None
        if latest is not None:
            target_idx = next((i for i, r in enumerate(series) if r["date"] == latest["date"]), None)
            if target_idx is not None and target_idx >= 30:
                for i in range(target_idx - 30, max(0, target_idx - 33), -1):
                    if series[i]["value"] is not None:
                        delta_30d = round(latest["value"] - series[i]["value"], 4)
                        break

        out = {
            "source": source,
            "field": "SplyExNtv",
            "last_updated": series[-1]["date"] if series else None,
            "history_start": series[0]["date"] if series else None,
            "rows": total_rows,
            "fetched_at": datetime.now().isoformat(),
            "series": {"exchange_reserve_btc": series},
            "latest": {
                "date": latest["date"] if latest else None,
                "exchange_reserve_btc": latest["value"] if latest else None,
                "exchange_reserve_btc_30d_delta": delta_30d,
            },
        }
        with open(OUT, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

        print(
            f"OK fetch_coinmetrics: rows={total_rows} · "
            f"history {out['history_start']} → {out['last_updated']} · "
            f"latest non-null = {latest['date']} → {latest['value']:,.0f} BTC · "
            f"30d Δ {delta_30d:+,.0f} BTC · source={source.split(' ')[0]}"
        )
        return 0
    except Exception as e:
        print(f"FAIL fetch_coinmetrics: {e}", file=sys.stderr)
        with open(OUT, "w") as f:
            json.dump({
                "source": source, "field": "SplyExNtv",
                "error": str(e), "fetched_at": datetime.now().isoformat(),
            }, f, ensure_ascii=False, indent=2)
        return 1


if __name__ == "__main__":
    sys.exit(main())
