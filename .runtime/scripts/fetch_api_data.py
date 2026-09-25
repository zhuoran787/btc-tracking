"""
fetch_api_data.py — BTC tracking: prices, correlations, funding rate, stablecoin supply

Data sources (all free, no API key):
  - CoinGecko: BTC price (USD), Gold price
  - yfinance: SOX (Philadelphia Semiconductor Index)
  - Hyperliquid: BTC funding rate (1h) + OI
  - DefiLlama: USDT + USDC market cap
"""

import json
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests
import yfinance as yf

CACHE_DIR = Path(__file__).parent.parent / "cache"
DATA_DIR = Path(__file__).parent.parent / "data"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
DEFILLAMA_BASE = "https://stablecoins.llama.fi"


def _get_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=20)
            if resp.status_code == 429:
                time.sleep(30)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f"GET {url} failed: {e}")


def _post_json(url, body, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=body, timeout=20)
            if resp.status_code == 429:
                time.sleep(5)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f"POST {url} failed: {e}")


# ── CoinGecko: BTC price + Gold price ──────────────────────────────

def fetch_btc_price_history(days=90):
    data = _get_json(f"{COINGECKO_BASE}/coins/bitcoin/market_chart",
                     {"vs_currency": "usd", "days": days, "interval": "daily"})
    prices = [(ts, price) for ts, price in data["prices"]]
    return prices


def fetch_gold_price_history(days=90):
    # CoinGecko: get BTC price in XAU (gold ounces), then derive gold USD price
    # Simpler: just get gold price via CoinGecko commodity endpoint
    try:
        data = _get_json(f"{COINGECKO_BASE}/coins/tether-gold/market_chart",
                         {"vs_currency": "usd", "days": days, "interval": "daily"})
        prices = [(ts, price) for ts, price in data["prices"]]
        if prices:
            return prices
    except Exception:
        pass
    # Fallback: yfinance
    try:
        ticker = yf.Ticker("GC=F")
        end = datetime.now()
        start = end - timedelta(days=days + 5)
        df = ticker.history(start=start.strftime("%Y-%m-%d"),
                            end=end.strftime("%Y-%m-%d"))
        if not df.empty:
            result = []
            for idx, row in df.iterrows():
                ts = int(idx.timestamp() * 1000)
                result.append((ts, float(row["Close"])))
            return result
    except Exception:
        pass
    print("WARNING: Could not fetch gold price from any source")
    return []


# ── yfinance: SOX ──────────────────────────────────────────────────

def fetch_sox_history(days=90):
    """抓 PHLX 半导体指数 ^SOX。
    SOX 真实价位在 2024+ 应 > 2000；SOXX ETF 是 ~$200-600 的 1/10 替代品。
    Sanity check：拿到数据后验证 latest close > 2000，否则视为'拿错 ticker'告警。
    旧版静默 fallback 到 SOXX 会导致数据错 10-23x（2026-05-20 用户发现）。"""
    SOX_MIN_VALID = 2000.0  # SOX 指数实际范围 4000-12000
    end = datetime.now()
    start = end - timedelta(days=days + 5)

    def _try_ticker(name):
        try:
            df = yf.Ticker(name).history(
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
            )
            if df.empty:
                return None, "empty"
            latest = float(df["Close"].iloc[-1])
            result = [(int(idx.timestamp() * 1000), float(row["Close"])) for idx, row in df.iterrows()]
            return result, latest
        except Exception as e:
            return None, f"exception: {e}"

    # 主路径：^SOX
    data, info = _try_ticker("^SOX")
    if data and isinstance(info, float) and info >= SOX_MIN_VALID:
        return data
    print(f"WARNING: ^SOX 抓取异常或值过低 (info={info}); 拒绝 fallback 到 SOXX 以避免 10x 数据错误。")
    # 不再静默 fallback——返回空让上层模板显示数据缺失而非错值
    return []


# ── Hyperliquid: Funding Rate + OI ────────────────────────────────

def fetch_funding_rate_history(days=90):
    now = int(datetime.now().timestamp() * 1000)
    all_rates = []
    # ⚠️ 2026-05-21：chunk_days 必须 ≤ ~20 天，否则触发 Hyperliquid fundingHistory 500 条 hard limit
    # 30 天 × 24h = 720 条 > 500 → 每个 chunk 只返回最早 500 条，最新 ~9 天数据被截断（导致 history 末端永远滞后 9-10 天）
    # 14 天 × 24h = 336 条，安全低于 500 limit
    chunk_days = 14
    for i in range((days // chunk_days) + 1):
        end_ts = now - i * chunk_days * 86400_000
        start_ts = end_ts - chunk_days * 86400_000
        body = {"type": "fundingHistory", "coin": "BTC",
                "startTime": start_ts, "endTime": end_ts}
        data = _post_json(HYPERLIQUID_URL, body)
        all_rates.extend(data)
        time.sleep(0.3)

    seen = set()
    unique = []
    for r in all_rates:
        if r["time"] not in seen:
            seen.add(r["time"])
            unique.append(r)
    unique.sort(key=lambda x: x["time"])
    return unique


def fetch_current_oi():
    ctx = _post_json(HYPERLIQUID_URL, {"type": "metaAndAssetCtxs"})
    meta, asset_ctxs = ctx[0], ctx[1]
    for i, coin_info in enumerate(meta["universe"]):
        if coin_info["name"] == "BTC":
            c = asset_ctxs[i]
            return {
                "funding_rate": float(c["funding"]) * 100,
                "open_interest_usd": float(c["openInterest"]) * float(c["markPx"]),
                "mark_price": float(c["markPx"]),
            }
    raise RuntimeError("BTC not found in Hyperliquid contexts")


# ── DefiLlama: Stablecoin Supply ──────────────────────────────────

def fetch_stablecoin_supply():
    data = _get_json(f"{DEFILLAMA_BASE}/stablecoins?includePrices=false")
    result = {}
    for coin in data.get("peggedAssets", []):
        symbol = coin.get("symbol", "")
        if symbol in ("USDT", "USDC"):
            chain_circ = coin.get("chainCirculating", {})
            total = 0
            for chain_data in chain_circ.values():
                peg = chain_data.get("current", {}).get("peggedUSD", 0)
                total += peg
            result[symbol] = {
                "current_mcap": total,
                "id": coin.get("id"),
            }
    return result


def fetch_stablecoin_history(coin_id, days=90):
    data = _get_json(f"{DEFILLAMA_BASE}/stablecoincharts/all",
                     {"stablecoin": coin_id})
    cutoff = int((datetime.now() - timedelta(days=days)).timestamp())
    history = []
    for point in data:
        ts = point.get("date", 0)
        if isinstance(ts, str):
            ts = int(ts)
        if ts >= cutoff:
            mcap = point.get("totalCirculating", {}).get("peggedUSD", 0)
            history.append({"ts": ts * 1000, "mcap": mcap})
    return history


# ── Calculations ──────────────────────────────────────────────────

def align_daily_series(series_a, series_b):
    """Align two price series on common UTC dates and drop invalid prices."""
    a_by_date = {}
    for ts, val in series_a:
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(val) or val <= 0:
            continue
        d = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
        a_by_date[d] = val
    b_by_date = {}
    for ts, val in series_b:
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if not np.isfinite(val) or val <= 0:
            continue
        d = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
        b_by_date[d] = val

    common_dates = sorted(set(a_by_date) & set(b_by_date))
    a_vals = np.array([a_by_date[d] for d in common_dates])
    b_vals = np.array([b_by_date[d] for d in common_dates])
    return common_dates, a_vals, b_vals


def compute_return_correlation(common_dates, a_vals, b_vals, observations):
    """Pearson correlation of aligned daily simple returns.

    ``observations`` is the requested number of return observations, so the
    calculation requires observations + 1 common-date price points.
    """
    required_prices = observations + 1
    if len(a_vals) < required_prices or len(b_vals) < required_prices:
        return None, {
            "observations": 0,
            "requested_observations": observations,
            "price_points": min(len(a_vals), len(b_vals)),
            "method": "Pearson correlation of daily simple returns",
        }

    dates_w = common_dates[-required_prices:]
    a_w = np.asarray(a_vals[-required_prices:], dtype=float)
    b_w = np.asarray(b_vals[-required_prices:], dtype=float)
    a_returns = a_w[1:] / a_w[:-1] - 1.0
    b_returns = b_w[1:] / b_w[:-1] - 1.0
    valid = np.isfinite(a_returns) & np.isfinite(b_returns)
    a_returns = a_returns[valid]
    b_returns = b_returns[valid]

    meta = {
        "observations": int(valid.sum()),
        "requested_observations": observations,
        "price_points": required_prices,
        "start_date": dates_w[0],
        "end_date": dates_w[-1],
        "method": "Pearson correlation of daily simple returns",
    }
    if len(a_returns) != observations or np.std(a_returns) == 0 or np.std(b_returns) == 0:
        return None, meta
    return float(np.corrcoef(a_returns, b_returns)[0, 1]), meta


def compute_change_pct(series, days):
    if len(series) < 2:
        return None
    current = series[-1]
    target_idx = max(0, len(series) - days)
    past = series[target_idx]
    if past == 0:
        return None
    return float((current - past) / past * 100)


def compute_funding_percentile(funding_history, window_days=90):
    """[DEPRECATED] 用 history_8h 的 compute_funding_percentile_8h 替代。
    旧逻辑用 hourly raw 但 dashboard 展示 8h sum，scale 不一致 → 失真。"""
    cutoff = int((datetime.now() - timedelta(days=window_days)).timestamp() * 1000)
    rates = [float(r["fundingRate"]) * 100 for r in funding_history
             if r["time"] >= cutoff]
    if not rates:
        return None
    current = rates[-1]
    percentile = sum(1 for r in rates if r <= current) / len(rates) * 100
    return round(percentile, 1)


def compute_funding_percentile_8h(history_8h, window_days=90):
    """口径与 dashboard 图表一致：用 history_8h.rate_8h（8 小时 sum）。
    一天 3 个窗口；窗口数不足时返回 None。
    """
    if not history_8h:
        return None
    n = window_days * 3
    last_n = history_8h[-n:] if len(history_8h) >= n else history_8h
    rates = [w["rate_8h"] for w in last_n]
    if not rates:
        return None
    current = rates[-1]
    return round(sum(1 for r in rates if r <= current) / len(rates) * 100, 1)


def aggregate_funding_8h(hourly_data):
    """Aggregate hourly funding rates into 8h windows."""
    from collections import defaultdict
    windows = defaultdict(list)
    for r in hourly_data:
        dt = datetime.fromtimestamp(r["time"] / 1000)
        bucket = dt.replace(hour=(dt.hour // 8) * 8, minute=0, second=0, microsecond=0)
        windows[bucket].append(float(r["fundingRate"]) * 100)

    result = []
    for bucket in sorted(windows):
        rates = windows[bucket]
        if len(rates) >= 6:
            result.append({
                "ts": int(bucket.timestamp() * 1000),
                "date": bucket.strftime("%Y-%m-%d %H:%M"),
                "rate_8h": sum(rates),
                "n_hours": len(rates),
            })
    return result


# ── Main ──────────────────────────────────────────────────────────

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    output = {}

    print("Fetching BTC price...", flush=True)
    btc_prices = fetch_btc_price_history(365)
    btc_current = btc_prices[-1][1] if btc_prices else 0
    output["btc_price"] = {
        "current": btc_current,
        "history": [{"ts": ts, "price": p} for ts, p in btc_prices],
        "change_7d": compute_change_pct([p for _, p in btc_prices], 7),
        "change_30d": compute_change_pct([p for _, p in btc_prices], 30),
        "change_90d": compute_change_pct([p for _, p in btc_prices], 90),
    }

    print("Fetching Gold price...", flush=True)
    time.sleep(1)
    gold_prices = fetch_gold_price_history(365)
    output["gold_price"] = {
        "current": gold_prices[-1][1] if gold_prices else 0,
        "history": [{"ts": ts, "price": p} for ts, p in gold_prices],
    }

    print("Fetching SOX index...", flush=True)
    sox_prices = fetch_sox_history(365)
    output["sox_price"] = {
        "current": sox_prices[-1][1] if sox_prices else 0,
        "history": [{"ts": ts, "price": p} for ts, p in sox_prices],
    }

    # Correlations
    print("Computing correlations...", flush=True)
    dates_bg, btc_arr, gold_arr = align_daily_series(btc_prices, gold_prices)
    dates_bs, btc_arr2, sox_arr = align_daily_series(btc_prices, sox_prices)

    bg_3m, bg_3m_meta = compute_return_correlation(dates_bg, btc_arr, gold_arr, 63)
    bg_6m, bg_6m_meta = compute_return_correlation(dates_bg, btc_arr, gold_arr, 126)
    bs_3m, bs_3m_meta = compute_return_correlation(dates_bs, btc_arr2, sox_arr, 63)
    bs_6m, bs_6m_meta = compute_return_correlation(dates_bs, btc_arr2, sox_arr, 126)
    output["correlations"] = {
        "btc_gold_3m": bg_3m,
        "btc_gold_6m": bg_6m,
        "btc_sox_3m": bs_3m,
        "btc_sox_6m": bs_6m,
    }
    output["correlations_meta"] = {
        "btc_gold_3m": bg_3m_meta,
        "btc_gold_6m": bg_6m_meta,
        "btc_sox_3m": bs_3m_meta,
        "btc_sox_6m": bs_6m_meta,
    }

    # Funding rate — 2 年 (730d) 满足 PPT 因素 6 图 1 时间跨度要求
    print("Fetching Hyperliquid funding rate (730d / 2 years)...", flush=True)
    funding_raw = fetch_funding_rate_history(730)
    funding_8h = aggregate_funding_8h(funding_raw)
    current_oi = fetch_current_oi()

    # 8h-window 口径修正：current_rate_pct 用最近 8h sum（与 history_8h 同源），
    # percentile 用 history_8h 算，避免 raw hourly vs 8h sum scale 不一致导致 100% 失真。
    latest_8h_rate = funding_8h[-1]["rate_8h"] if funding_8h else current_oi["funding_rate"]
    output["funding"] = {
        "source": "Hyperliquid",
        "current_rate_pct": latest_8h_rate,
        "current_rate_pct_hourly_snapshot": current_oi["funding_rate"],
        "current_oi_usd": current_oi["open_interest_usd"],
        "mark_price": current_oi["mark_price"],
        "percentile_90d": compute_funding_percentile_8h(funding_8h, 90),
        "percentile_730d": compute_funding_percentile_8h(funding_8h, 730),
        "history_8h": funding_8h,
        "negative_streak": _find_negative_streaks(funding_8h),
    }

    # Stablecoin supply
    print("Fetching stablecoin supply (DefiLlama)...", flush=True)
    stable_current = fetch_stablecoin_supply()
    stable_history = {}
    for symbol in ("USDT", "USDC"):
        if symbol in stable_current and stable_current[symbol].get("id"):
            time.sleep(1)
            hist = fetch_stablecoin_history(stable_current[symbol]["id"], 365)
            stable_history[symbol] = hist

    usdt_mcap = stable_current.get("USDT", {}).get("current_mcap", 0)
    usdc_mcap = stable_current.get("USDC", {}).get("current_mcap", 0)
    total_mcap = usdt_mcap + usdc_mcap

    combined_history = _combine_stablecoin_history(
        stable_history.get("USDT", []),
        stable_history.get("USDC", []))

    output["stablecoins"] = {
        "usdt_mcap": usdt_mcap,
        "usdc_mcap": usdc_mcap,
        "total_mcap": total_mcap,
        "change_7d": _stablecoin_change(combined_history, 7),
        "change_30d": _stablecoin_change(combined_history, 30),
        "change_1y": _stablecoin_change(combined_history, 365),
        "history": combined_history,
    }

    output["generated_at"] = datetime.now().isoformat()

    out_path = DATA_DIR / "api_data.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"Saved to {out_path}")
    return output


def _find_negative_streaks(funding_8h):
    streaks = []
    count = 0
    start_date = None
    for item in funding_8h:
        if item["rate_8h"] < 0:
            if count == 0:
                start_date = item["date"]
            count += 1
        else:
            if count >= 3:
                streaks.append({"start": start_date, "windows": count,
                                "days": round(count / 3, 1)})
            count = 0
    if count >= 3:
        streaks.append({"start": start_date, "windows": count,
                        "days": round(count / 3, 1)})
    return streaks


def _combine_stablecoin_history(usdt_hist, usdc_hist):
    by_date = {}
    for item in usdt_hist:
        d = datetime.fromtimestamp(item["ts"] / 1000).strftime("%Y-%m-%d")
        by_date[d] = {"ts": item["ts"], "usdt": item["mcap"], "usdc": 0}
    for item in usdc_hist:
        d = datetime.fromtimestamp(item["ts"] / 1000).strftime("%Y-%m-%d")
        if d in by_date:
            by_date[d]["usdc"] = item["mcap"]
        else:
            by_date[d] = {"ts": item["ts"], "usdt": 0, "usdc": item["mcap"]}
    result = []
    for d in sorted(by_date):
        entry = by_date[d]
        entry["total"] = entry["usdt"] + entry["usdc"]
        entry["date"] = d
        result.append(entry)
    return result


def _stablecoin_change(history, days):
    if len(history) < 2:
        return None
    current = history[-1]["total"]
    idx = max(0, len(history) - days)
    past = history[idx]["total"]
    if past == 0:
        return None
    return round((current - past) / past * 100, 2)


if __name__ == "__main__":
    main()
