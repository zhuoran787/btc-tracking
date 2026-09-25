"""Fetch derivatives data: full-market aggregate (current) + OKX OI history (730d).

- CoinGecko /derivatives: current snapshot of all BTC perpetuals across exchanges
  -> aggregate OI + OI-weighted funding rate (full market signal NOW)
- OKX /rubik/stat/contracts/open-interest-history: paginated daily OI history (single
  exchange but real time-series). Endpoint hard cap = 100 days per call, but 用 end
  时间游标分页可以拉到 OKX 上限 ~2.4 年（2023-12-31 起）。

Output: data/derivatives_data.json
"""
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "derivatives_data.json"
DB = ROOT / "data" / "derivatives_history.sqlite"

# ── OKX OI 历史拉取长度（改这一个 = 函数默认 / main 调用 / 日志全部同步）─────────
# OKX endpoint 上限 ~2.4 年（最老 2023-12-31），730d (2 年) 覆盖足够 + ~8 次 API 调用 ~3 秒
OKX_OI_DAYS = 730

# ── Coinalyze 跨所聚合 OI（2026-07-03 加，TODO #10 解决）────────
# 免费 API key 从 .env 读（COINALYZE_API_KEY，用户 2026-07-02 注册）；无 key 时静默跳过（向后兼容）
# 头部 6 所 9 个 BTC 永续合约（Binance USDT/USD + Bybit USDT/USD + OKX USDT/USD + Deribit + Hyperliquid + Bitget）
# daily 历史 Coinalyze 上限 = 最近 ~1500 天（2022-05 起，4 年+），40 req/min
COINALYZE_URL = "https://api.coinalyze.net/v1/open-interest-history"
COINALYZE_SYMBOLS = ("BTCUSDT_PERP.A,BTCUSD_PERP.A,BTCUSDT.6,BTCUSD.6,"
                     "BTCUSDT_PERP.3,BTCUSD_PERP.3,BTC-PERPETUAL.2,BTC.H,BTCUSDT_PERP.F")


def fetch_coingecko_aggregate() -> dict:
    url = "https://api.coingecko.com/api/v3/derivatives?include_tickers=unexpired"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    tickers = r.json()

    # Filter BTC perpetuals (USD-margined and USDT-margined both count)
    btc_perps = [
        t for t in tickers
        if t.get("contract_type") == "perpetual"
        and any(x in t.get("symbol", "").upper() for x in ("BTC", "XBT"))
    ]

    total_oi_usd = 0.0
    weighted_fr_num = 0.0
    weighted_fr_den = 0.0
    by_exchange: list[dict] = []
    for t in btc_perps:
        oi = float(t.get("open_interest") or 0)
        fr = t.get("funding_rate")
        market = t.get("market", "")
        if oi <= 0:
            continue
        total_oi_usd += oi
        if fr is not None:
            try:
                fr_f = float(fr)
                weighted_fr_num += fr_f * oi
                weighted_fr_den += oi
            except (TypeError, ValueError):
                pass
        by_exchange.append({
            "exchange": market,
            "symbol": t.get("symbol"),
            "oi_usd": oi,
            "funding_rate_pct": fr,
            "volume_24h": t.get("converted_volume", {}).get("usd") if isinstance(t.get("converted_volume"), dict) else None,
        })

    by_exchange.sort(key=lambda x: x["oi_usd"], reverse=True)

    return {
        "total_oi_usd": total_oi_usd,
        "oi_weighted_funding_pct": (weighted_fr_num / weighted_fr_den) if weighted_fr_den else None,
        "num_exchanges": len(by_exchange),
        "top_exchanges": by_exchange[:15],
    }


def fetch_okx_oi_history(days: int = OKX_OI_DAYS) -> dict:
    """OKX BTC-USDT-SWAP daily OI history with pagination.

    OKX `/rubik/stat/contracts/open-interest-history` limit per request = 100 days hard cap
    (limit=200/500/1000 all return 100). 用 `end` 时间游标分页拉到 ~2.4 年历史。
    OKX 这个 endpoint 最老到 2023-12-31，再往前无数据。
    """
    import time
    url = "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history"
    all_rows = []
    end_ts = None
    max_pages = (days // 100) + 2  # 留 buffer
    for _ in range(max_pages):
        params = {"instId": "BTC-USDT-SWAP", "period": "1D", "limit": 100}
        if end_ts is not None:
            params["end"] = str(end_ts)
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            page = r.json().get("data", [])
        except Exception:
            break
        if not page:
            break
        all_rows.extend(page)
        oldest_ts = int(page[-1][0])  # OKX returns newest first
        end_ts = oldest_ts  # 下一页拉这之前的
        time.sleep(0.3)
        # 已经覆盖 days 就提前停（多 50 天 buffer 防边界）
        if len(all_rows) >= days + 50:
            break

    # OKX returns [ts_ms, contracts, oi_btc, oi_usd], newest first; 去重
    seen_ts = set()
    series = []
    for row in all_rows:
        ts_ms = int(row[0])
        if ts_ms in seen_ts:
            continue
        seen_ts.add(ts_ms)
        d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()
        series.append({
            "date": d,
            "oi_btc": float(row[2]),
            "oi_usd": float(row[3]),
        })
    series.sort(key=lambda x: x["date"])  # oldest first
    # 裁到最近 days 天（如分页拉超过 buffer）
    if len(series) > days:
        series = series[-days:]

    return {
        "instrument": "BTC-USDT-SWAP @ OKX",
        "days": len(series),
        "dates": [s["date"] for s in series],
        "oi_btc": [s["oi_btc"] for s in series],
        "oi_usd": [s["oi_usd"] for s in series],
        "latest_oi_usd": series[-1]["oi_usd"] if series else None,
    }


def persist_aggregate(agg: dict, snapshot_date: str) -> list[dict]:
    """Persist daily aggregate snapshot to SQLite. After 2-3 weeks builds full-market
    OI + OI-weighted funding history that Coinglass otherwise charges for."""
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_aggregate (
            date TEXT PRIMARY KEY,
            total_oi_usd REAL,
            oi_weighted_funding_pct REAL,
            num_exchanges INTEGER
        )
        """
    )
    if "error" not in agg:
        conn.execute(
            "INSERT OR REPLACE INTO market_aggregate "
            "(date, total_oi_usd, oi_weighted_funding_pct, num_exchanges) "
            "VALUES (?, ?, ?, ?)",
            (
                snapshot_date,
                agg.get("total_oi_usd"),
                agg.get("oi_weighted_funding_pct"),
                agg.get("num_exchanges"),
            ),
        )
        conn.commit()
    cur = conn.execute(
        "SELECT date, total_oi_usd, oi_weighted_funding_pct, num_exchanges "
        "FROM market_aggregate ORDER BY date"
    )
    history = [
        {"date": d, "total_oi_usd": oi, "funding_pct": fr, "num_exchanges": n}
        for d, oi, fr, n in cur.fetchall()
    ]
    conn.close()
    return history


def _load_coinalyze_key() -> str | None:
    env = ROOT / ".env"
    if not env.exists():
        return None
    for line in env.read_text().splitlines():
        if line.startswith("COINALYZE_API_KEY="):
            return line.split("=", 1)[1].strip() or None
    return None


def fetch_coinalyze_oi_agg() -> dict:
    """头部 6 所 BTC 永续 OI daily 聚合（USD）。一次请求 9 个 symbol，服务端返回各自 history 后本地加总。"""
    import time as _t
    from collections import defaultdict
    from datetime import datetime as _dt
    key = _load_coinalyze_key()
    if not key:
        return {"skipped": "no COINALYZE_API_KEY in .env"}
    r = requests.get(COINALYZE_URL, headers={"api_key": key}, params={
        "symbols": COINALYZE_SYMBOLS, "interval": "daily",
        "from": 1577836800, "to": int(_t.time()), "convert_to_usd": "true",
    }, timeout=90)
    r.raise_for_status()
    agg = defaultdict(float)
    n_syms = 0
    for item in r.json():
        hist = item.get("history") or []
        if hist:
            n_syms += 1
        for pt in hist:
            agg[_dt.fromtimestamp(pt["t"]).date().isoformat()] += pt["c"]
    days = sorted(agg)
    if len(days) < 500:
        raise ValueError(f"coinalyze agg too short ({len(days)} days)")
    return {
        "source": "coinalyze.net (top-6 exchanges, 9 BTC perp contracts)",
        "days": len(days),
        "dates": days,
        "oi_usd": [round(agg[d], 0) for d in days],
        "latest_oi_usd": round(agg[days[-1]], 0),
        "n_symbols": n_syms,
    }


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    today = date.today().isoformat()

    print("CoinGecko full-market aggregate...", file=sys.stderr)
    try:
        agg = fetch_coingecko_aggregate()
    except Exception as exc:
        agg = {"error": str(exc)}

    print(f"OKX OI {OKX_OI_DAYS}d history (paginated, ~{(OKX_OI_DAYS // 100) + 2} API calls)...", file=sys.stderr)
    try:
        okx = fetch_okx_oi_history()  # 用默认 OKX_OI_DAYS
    except Exception as exc:
        okx = {"error": str(exc)}

    market_history = persist_aggregate(agg, today)

    print("Coinalyze top-6 exchange OI aggregate (4y daily)...", file=sys.stderr)
    try:
        coinalyze = fetch_coinalyze_oi_agg()
    except Exception as exc:
        coinalyze = {"error": str(exc)}

    output = {
        "fetched_at": today,
        "coingecko_aggregate": agg,
        "okx_oi_history": okx,
        "coinalyze_oi_agg": coinalyze,
        "market_aggregate_history": market_history,
        "market_history_days": len(market_history),
    }
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(
        f"OK: wrote {OUT} "
        f"(market OI=${agg.get('total_oi_usd', 0)/1e9:.1f}B, "
        f"OKX history days={okx.get('days', 0)}, "
        f"coinalyze agg days={coinalyze.get('days', 0)}, "
        f"market history days accumulated={len(market_history)})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
