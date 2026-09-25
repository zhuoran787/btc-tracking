"""Self-synthesize ahr999 大周期估值指标（PPT 因素 6 图 3）。

公式来源：PPT image14 caption（CRSHK compiled）。
  ahr999 = (current_price / geometric_mean_200d) · (current_price / fair_price_by_age)
其中：
  fair_price_by_age = 10^[5.84 · log10(coin_age_days) - 17.01]
  coin_age_days     = 自创世日 2009-01-03 到今天的累计天数

阈值参考：≤ 0.45 = "buy the dip"；≥ 1.0 = "over-value"。

数据来源：blockchain.com 历史日价（10 年免费 API），CoinGecko free tier
只回 365 天不够算 200d 均线 + 多年回溯，故弃用。

Output: data/ahr999.json
"""
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "ahr999.json"

GENESIS = date(2009, 1, 3)
API = "https://api.blockchain.info/charts/market-price?timespan=10years&format=json&sampled=false"


def fetch_btc_history():
    """Return list of (date_iso, price_usd) ascending."""
    r = requests.get(API, timeout=30)
    r.raise_for_status()
    j = r.json()
    pts = []
    for v in j.get("values", []):
        ts = v.get("x")
        px = v.get("y")
        if ts is None or px is None or px <= 0:
            continue
        d = date.fromtimestamp(ts)
        pts.append((d.isoformat(), float(px)))
    # 按日期升序
    pts.sort(key=lambda x: x[0])
    # 去重（同日多条取最后）
    dedup = {}
    for d, p in pts:
        dedup[d] = p
    return sorted(dedup.items(), key=lambda x: x[0])


def geometric_mean_200d(window):
    """Compute geometric mean of a 200-element price window."""
    if not window:
        return None
    # log avg → exp（避免大数值乘法溢出）
    s = sum(math.log(p) for p in window)
    return math.exp(s / len(window))


def coin_age_days(d_iso):
    """自 2009-01-03 创世日累计天数。"""
    d = date.fromisoformat(d_iso)
    return max(1, (d - GENESIS).days)


def fair_price_by_age(age_days):
    """fair_price = 10^[5.84 · log10(coin_age_days) - 17.01]."""
    return 10 ** (5.84 * math.log10(age_days) - 17.01)


def compute_ahr999(history):
    """Return list of dicts {date, price, ahr999, fair}; only days that have ≥200d prior history."""
    out = []
    prices = [p for _, p in history]
    dates = [d for d, _ in history]
    for i in range(200, len(history)):
        window = prices[i - 200:i]
        gm = geometric_mean_200d(window)
        cur_price = prices[i]
        cur_date = dates[i]
        age = coin_age_days(cur_date)
        fair = fair_price_by_age(age)
        if gm <= 0 or fair <= 0:
            continue
        ratio1 = cur_price / gm
        ratio2 = cur_price / fair
        ahr = ratio1 * ratio2
        out.append({"date": cur_date, "price": cur_price, "ahr999": ahr, "fair": fair})
    return out


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    try:
        hist = fetch_btc_history()
    except Exception as e:
        print(f"FAIL fetch_ahr999 (blockchain.com): {e}", file=sys.stderr)
        return 1

    if len(hist) < 250:
        print(f"FAIL fetch_ahr999: history too short ({len(hist)} days)", file=sys.stderr)
        return 1

    series = compute_ahr999(hist)
    if not series:
        print("FAIL fetch_ahr999: no rows after 200d warmup", file=sys.stderr)
        return 1

    cur = series[-1]
    out = {
        "current": cur["ahr999"],
        "current_date": cur["date"],
        "current_price": cur["price"],
        "fair_price": cur["fair"],
        "threshold_low": 0.45,
        "threshold_high": 1.0,
        "dates": [r["date"] for r in series],
        "values": [round(r["ahr999"], 4) for r in series],
        "btc_prices": [round(r["price"], 2) for r in series],
        "source": "blockchain.com 历史日价 + 自合成公式 (CRSHK compiled)",
        "fetched_at": datetime.now().isoformat(),
    }

    with open(OUT, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    label = "buy-the-dip" if cur["ahr999"] <= 0.45 else ("over-value" if cur["ahr999"] >= 1.0 else "中性区")
    print(
        f"OK fetch_ahr999: {cur['ahr999']:.3f} ({label}) · BTC ${cur['price']:,.0f} "
        f"vs fair ${cur['fair']:,.0f} · 共 {len(series)} 天历史 -> {OUT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
