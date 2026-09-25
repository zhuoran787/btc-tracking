"""Fetch Crypto Fear & Greed Index — alternative.me free public API.

PPT 因素 6 图 2 数据源。100% 免费、无 key、JSON 返回。

Output: data/fear_greed.json
{
  "current": 28,
  "classification": "Fear",
  "classification_zh": "恐惧",
  "values": [28, 27, 31, ...],  # 90 天历史，最早 → 最新
  "dates": ["2026-02-17", ...],
  "timestamp": 1779062400,
}
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "fear_greed.json"

API = "https://api.alternative.me/fng/?limit=1200&format=json"  # PPT 要求 ≥ 1 年；alternative.me 自 2018 起，1200 天 ≈ 3.3 年

# alternative.me 用 5 个 classification：Extreme Fear / Fear / Neutral / Greed / Extreme Greed
ZH_MAP = {
    "Extreme Fear": "极度恐惧",
    "Fear": "恐惧",
    "Neutral": "中性",
    "Greed": "贪婪",
    "Extreme Greed": "极度贪婪",
}


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    try:
        r = requests.get(API, timeout=15)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        print(f"FAIL fetch_fear_greed: {e}", file=sys.stderr)
        return 1

    items = raw.get("data") or []
    if not items:
        print("FAIL fetch_fear_greed: empty data", file=sys.stderr)
        return 1

    # API 返回最新 → 最早，反转为最早 → 最新
    items_oldest_first = list(reversed(items))
    values = [int(x["value"]) for x in items_oldest_first]
    dates = [
        datetime.fromtimestamp(int(x["timestamp"]), tz=timezone.utc).date().isoformat()
        for x in items_oldest_first
    ]

    latest = items[0]
    cur_val = int(latest["value"])
    cur_cls = latest["value_classification"]

    out = {
        "current": cur_val,
        "classification": cur_cls,
        "classification_zh": ZH_MAP.get(cur_cls, cur_cls),
        "values": values,
        "dates": dates,
        "timestamp": int(latest["timestamp"]),
        "source": "alternative.me/fng",
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }

    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"OK fetch_fear_greed: {cur_val} ({cur_cls} / {out['classification_zh']}) · {len(values)} 天历史 -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
