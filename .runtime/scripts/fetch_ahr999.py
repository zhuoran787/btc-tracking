"""Self-synthesize ahr999 大周期估值指标（PPT 因素 6 图 3）。

公式来源：PPT image14 caption（CRSHK compiled）。
  ahr999 = (current_price / geometric_mean_200d) · (current_price / fair_price_by_age)
其中：
  fair_price_by_age = 10^[5.84 · log10(coin_age_days) - 17.01]
  coin_age_days     = 自创世日 2009-01-03 到今天的累计天数

阈值参考：≤ 0.45 = "buy the dip"；≥ 1.0 = "over-value"。

数据来源：Coin Metrics Community PriceUSD，免费完整日价历史。
PriceUSD 的日期 D 表示 D 日末价格；按 D+1 的 UTC 零点标记，
与原 Blockchain.com 的零点价格时刻对齐。整段重算，不拼接旧源。

Output: data/ahr999.json
"""
import json
import hashlib
import math
import os
import sys
import tempfile
from urllib.parse import urlparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "ahr999.json"
CONFIG = ROOT / "publication" / "config.json"

GENESIS = date(2009, 1, 3)
API = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
METRIC = "PriceUSD"
SOURCE_LABEL = "Coin Metrics Community PriceUSD（UTC 日终价，按次日零点标记）"
METHOD_ID = "ahr999_coinmetrics_priceusd_utc_midnight_v1"
PRICE_CLOCK = "00:00 UTC; PriceUSD period date + 1 calendar day"
WARMUP_DAYS = 200
HISTORY_YEARS = 10
AHR999_BOTTOM = 0.45
AHR999_TOP = 1.0


def validation_rules():
    config = json.loads(CONFIG.read_text())
    return next(s["validation"] for s in config["fetchers"]
                if s["output"] == OUT.name)


def rules_hash(rules):
    return hashlib.sha256(json.dumps(rules, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def parse_history(payload, today):
    """Normalize completed daily periods to their actual UTC fixing date."""
    if payload.get("next_page_url") or payload.get("next_page_token"):
        raise ValueError("Price history is paginated; refusing a truncated series")
    points = {}
    for row in payload.get("data", []):
        if row.get("asset") != "btc":
            raise ValueError("Unexpected asset in price history")
        stamp = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
        if stamp.utcoffset() != timedelta(0) or stamp.time().isoformat() != "00:00:00":
            raise ValueError("Expected a UTC daily period label")
        period = stamp.date()
        if period >= today:
            continue  # today's end-of-day fixing has not completed
        price = row.get(METRIC)
        if price is None:
            raise ValueError(f"Missing completed daily price: {period}")
        price = float(price)
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"Invalid price: {period}")
        observed = (period + timedelta(days=1)).isoformat()
        if observed in points and points[observed] != price:
            raise ValueError(f"Conflicting duplicate price: {observed}")
        points[observed] = price
    history = sorted(points.items())
    for (left, _), (right, _) in zip(history, history[1:]):
        if date.fromisoformat(right) - date.fromisoformat(left) != timedelta(days=1):
            raise ValueError(f"Missing daily price between {left} and {right}")
    return history


def fetch_btc_history(now=None):
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    try:
        start = today.replace(year=today.year - HISTORY_YEARS)
    except ValueError:
        start = today.replace(year=today.year - HISTORY_YEARS, day=28)
    response = requests.get(API, params={
        "assets": "btc", "metrics": METRIC, "frequency": "1d",
        "start_time": (start - timedelta(days=1)).isoformat(),
        "page_size": 10000,
    }, timeout=90)
    response.raise_for_status()
    history = parse_history(response.json(), today)
    return history, {"source_url": response.url,
                     "raw_sha256": hashlib.sha256(response.content).hexdigest()}


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
    for i in range(WARMUP_DAYS, len(history)):
        window = prices[i - WARMUP_DAYS:i]
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


def build_snapshot(hist, provenance, now=None):
    now = now or datetime.now(timezone.utc)
    series = compute_ahr999(hist)
    if not series:
        raise ValueError("No rows after price-history warmup")
    cur = series[-1]
    return {
        "current": cur["ahr999"],
        "current_date": cur["date"],
        "current_price": cur["price"],
        "fair_price": cur["fair"],
        "threshold_low": AHR999_BOTTOM,
        "threshold_high": AHR999_TOP,
        "dates": [r["date"] for r in series],
        "values": [round(r["ahr999"], 4) for r in series],
        "btc_prices": [r["price"] for r in series],
        "source": SOURCE_LABEL,
        "source_metric": METRIC,
        "source_clock": PRICE_CLOCK,
        "source_period_date": (date.fromisoformat(cur["date"]) - timedelta(days=1)).isoformat(),
        "methodology_id": METHOD_ID,
        "validation_config_hash": rules_hash(validation_rules()),
        "price_history_start": hist[0][0],
        "fetched_at": now.astimezone(timezone.utc).isoformat(),
        **provenance,
    }


def validate_snapshot(snapshot, rules=None, now=None):
    rules = rules if rules is not None else validation_rules()
    now = now or datetime.now(timezone.utc)
    if snapshot.get("methodology_id") != METHOD_ID or snapshot.get("source_metric") != METRIC:
        raise ValueError("ahr999 source or methodology is not Coin Metrics")
    if snapshot.get("source_clock") != PRICE_CLOCK:
        raise ValueError("ahr999 UTC price clock changed")
    source = urlparse(snapshot.get("source_url", ""))
    expected_source = urlparse(API)
    if source.scheme != expected_source.scheme or source.netloc != expected_source.netloc or source.path != expected_source.path:
        raise ValueError("ahr999 source URL does not match Coin Metrics")
    raw_hash = snapshot.get("raw_sha256", "")
    if len(raw_hash) != 64 or any(char not in "0123456789abcdef" for char in raw_hash):
        raise ValueError("ahr999 raw response hash is invalid")
    if snapshot.get("validation_config_hash") != rules_hash(rules):
        raise ValueError("ahr999 validation config changed; refetch data")
    dates = snapshot.get("dates", [])
    prices, values = snapshot.get("btc_prices", []), snapshot.get("values", [])
    if len(dates) < rules["lengths"]["dates"] or not len(dates) == len(prices) == len(values):
        raise ValueError("Insufficient or unaligned ahr999 history")
    if dates != sorted(set(dates)):
        raise ValueError("Duplicate or unsorted ahr999 dates")
    if any(date.fromisoformat(b) - date.fromisoformat(a) != timedelta(days=1)
           for a, b in zip(dates, dates[1:])):
        raise ValueError("ahr999 history contains missing days")
    if any(not math.isfinite(v) or v <= 0 for v in prices + values):
        raise ValueError("Invalid ahr999 value or price")
    if snapshot.get("current_date") != dates[-1] or snapshot.get("current_price") != prices[-1]:
        raise ValueError("ahr999 latest observation disagrees with history")
    observed = datetime.fromisoformat(dates[-1]).replace(tzinfo=timezone.utc)
    age = (now.astimezone(timezone.utc) - observed).total_seconds() / 86400
    if age < 0 or age > rules["max_age_days"]["current_date"]:
        raise ValueError("Stale or future ahr999 price observation")
    if snapshot.get("source_period_date") != (observed.date() - timedelta(days=1)).isoformat():
        raise ValueError("Price period and UTC observation date do not align")
    for key in rules.get("required", []):
        if not snapshot.get(key):
            raise ValueError(f"Missing ahr999 provenance: {key}")
    if not math.isclose(snapshot["current"], values[-1], abs_tol=0.00005):
        raise ValueError("ahr999 latest value disagrees with chart")
    # Rebuild the last value from the preceding price window, independently of dates/values metadata.
    current = prices[-1] / geometric_mean_200d(prices[-WARMUP_DAYS-1:-1])
    current *= prices[-1] / fair_price_by_age(coin_age_days(dates[-1]))
    if not math.isclose(snapshot["current"], current, rel_tol=1e-12):
        raise ValueError("ahr999 formula validation failed")


def main() -> int:
    temporary = None
    try:
        hist, provenance = fetch_btc_history()
        out = build_snapshot(hist, provenance)
        validate_snapshot(out)
        OUT.parent.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=OUT.parent, prefix=".ahr999-",
                                         suffix=".json", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(out, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, OUT)
    except Exception as exc:
        if temporary is not None and temporary.exists():
            temporary.unlink()  # only our own incomplete temporary output
        print(f"FAIL fetch_ahr999: {exc}; previous snapshot preserved", file=sys.stderr)
        return 1
    label = "buy-the-dip" if out["current"] <= out["threshold_low"] else (
        "over-value" if out["current"] >= out["threshold_high"] else "中性区")
    print(
        f"OK fetch_ahr999: {out['current']:.3f} ({label}) · BTC ${out['current_price']:,.0f} "
        f"· {len(out['dates'])} days through {out['current_date']} 00:00 UTC "
        f"· {SOURCE_LABEL} -> {OUT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
