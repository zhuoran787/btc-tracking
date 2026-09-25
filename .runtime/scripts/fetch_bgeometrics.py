"""Fetch CVDD (PPT 因素 6 图 4) + MVRV Z-Score (图 5).

数据源（2026-05-22 重构）：
- **CVDD：直接拉 Willy Woo 官方 chart.json**（CVDD 公式发明人本人维护）
    https://woocharts.com/bitcoin-price-models/data/chart.json
  一个 JSON 含 7 个估值模型（market/realised/delta/top_/vwap/wma200/cvdd），
  CVDD 6073 行 daily（2009-10-05 → today，每天更新），无需 key + 无限速 +
  跟主流口径完全对齐（验证：4/26 woocharts $45,813 vs 之前自合成 $48,043 差 -4.64%；
  跟 Bitget News 2 月报道 $47,516 量级一致）。
  ⚠️ 必须带浏览器 UA，否则 Cloudflare 返 HTTP 403。
  历史方案（已废，2026-05-22 切走）：从 charts.bgeometrics.com/files/cdd.json 自合成。
  该静态 JSON 4/26 后停更（BGeometrics 是一人项目，cron 静默挂掉），切走避免单点故障。

- MVRV Z-Score：BRK/Bitview 原始 market_cap、realized_cap 自算。
  使用截至每个历史日的全历史总体标准差（ddof=0），不使用未来数据。
  仅输出上一完整 UTC 日及更早日期；失败保留旧来源标签，不拼接供应商。
  文件名为兼容已有消费者保留，不再请求 BGeometrics。

阈值参考（用户 2026-05-21 定）：
  BTC Price / CVDD ≤ 1.03 = 大周期底部
  MVRV Z < 0   = 大周期底部信号
  MVRV Z ≥ 4.0 = 大周期顶部信号

Output: data/bgeometrics_data.json
"""
import json
import hashlib
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "bgeometrics_data.json"

# CVDD：Willy Woo 官方 7 模型 JSON（CVDD 公式发明人本人维护，daily 更新）
WOOCHARTS_URL = "https://woocharts.com/bitcoin-price-models/data/chart.json"
# Cloudflare 反爬必须带浏览器 UA + Referer
WOOCHARTS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json,*/*",
    "Referer": "https://woocharts.com/bitcoin-price-models/",
}

# MVRV raw series; order follows the documented bulk request order.
MVRV_Z_URL = "https://bitview.space/api/series/bulk?series=date,market_cap,realized_cap&index=day1&start=0"
MVRV_Z_METHOD = "brk_expanding_population_v1"
MVRV_Z_CHART_START = "2022-09-25"  # preserve the old chart start at migration

CVDD_BOTTOM_BAND = 1.03  # BTC Price / CVDD ≤ 1.03 视为逼近大周期底部
MVRV_Z_BOTTOM = 0.0      # z < 0 = 大周期底部
MVRV_Z_TOP = 4.0         # z ≥ 4.0 = 大周期顶部


def fetch_cvdd() -> dict:
    """从 Willy Woo 官方 chart.json 提取 CVDD 时间序列。

    JSON 结构：{ "cvdd": {"x": [ts_ms, ...], "y": [usd, ...]}, "market": {...}, ... }
    返回 {dates, values, current, current_date, source_note}.
    """
    r = requests.get(WOOCHARTS_URL, headers=WOOCHARTS_HEADERS, timeout=30)
    r.raise_for_status()
    full = r.json()
    cv = full.get("cvdd") or {}
    ts_list = cv.get("x") or []
    val_list = cv.get("y") or []
    if len(ts_list) < 1000 or len(val_list) < 1000:
        raise ValueError(f"woocharts cvdd too short (x={len(ts_list)}, y={len(val_list)})")

    series: list[dict] = []
    for ts_ms, val in zip(ts_list, val_list):
        if val is None:
            continue
        d_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        series.append({"date": d_str, "value": round(float(val), 4)})
    series.sort(key=lambda x: x["date"])
    if len(series) < 100:
        raise ValueError(f"CVDD series too short ({len(series)} rows)")

    latest = series[-1]
    return {
        "dates": [r["date"] for r in series],
        "values": [r["value"] for r in series],
        "current": latest["value"],
        "current_date": latest["date"],
        "source_note": "woocharts.com chart.json (Willy Woo official, CVDD inventor)",
    }


def calculate_mvrv_zscore(raw: list, today: date | None = None) -> dict:
    """Expand population variance through time; validate alignment before math."""
    today = today or datetime.now(timezone.utc).date()
    if len(raw) != 3 or len({(x['start'], x['end'], x['index'], x['stamp']) for x in raw}) != 1:
        raise ValueError('BRK bulk components are not one aligned snapshot')
    if raw[0]['start'] != 0 or raw[0]['index'] != 'day1':
        raise ValueError('BRK requires full day1 history from index zero')
    if [x['type'] for x in raw] != ['Date', 'Dollars', 'Dollars']:
        raise ValueError('BRK bulk series types/order changed')
    dates, market, realized = [x['data'] for x in raw]
    if not dates or len({len(dates), len(market), len(realized)}) != 1:
        raise ValueError('BRK dates and capitalizations have different lengths')
    if dates[0] != '2009-01-01':
        raise ValueError('BRK historical origin changed; review denominator before proceeding')
    parsed = [date.fromisoformat(d) for d in dates]
    if any(b-a != timedelta(days=1) for a,b in zip(parsed,parsed[1:])):
        raise ValueError('BRK calendar is duplicated, unordered or missing days')
    mean = m2 = 0.0
    n = 0
    price_started = False
    out_dates, values = [], []
    for day, market_cap, realized_cap in zip(parsed, market, realized):
        if day >= today:
            continue
        if market_cap is None or realized_cap is None:
            if price_started:
                raise ValueError(f'BRK capitalization missing after price history begins: {day}')
            continue  # null days before pricing began are not zeros
        m, r = float(market_cap), float(realized_cap)
        if not all(math.isfinite(x) and x >= 0 for x in (m,r)):
            raise ValueError(f'Invalid BRK capitalization on {day}')
        price_started = price_started or m > 0
        n += 1
        delta = m - mean
        mean += delta / n
        m2 += delta * (m - mean)
        sd = math.sqrt(max(0.0, m2 / n))
        if sd > 0:
            out_dates.append(day.isoformat())
            values.append(round((m-r)/sd, 8))
    if len(values) < 1000:
        raise ValueError('BRK calculated history too short')
    return {
        'dates': out_dates, 'values': values, 'current': values[-1],
        'current_date': out_dates[-1], 'chart_start': MVRV_Z_CHART_START,
        'source_url': MVRV_Z_URL, 'source_label': 'BRK / Bitview 原始数据自算',
        'methodology_id': MVRV_Z_METHOD,
        'methodology': '(market_cap - realized_cap) / expanding population std(market_cap), ddof=0; no future observations',
        'history_origin': dates[0], 'denominator_observations': n,
        'early_history': 'skip null days; include non-null zero market caps',
        'day_policy': 'completed UTC days only', 'source_stamp': raw[0]['stamp'],
        'raw_sha256': hashlib.sha256(json.dumps(raw, separators=(',',':')).encode()).hexdigest(),
        'delayed': False,
    }


def fetch_mvrv_zscore() -> dict:
    response = requests.get(MVRV_Z_URL, headers={'User-Agent': 'btc-tracking/1.0',
                                               'Accept': 'application/json'}, timeout=45)
    response.raise_for_status()
    return calculate_mvrv_zscore(response.json())


def _load_previous(key: str) -> dict | None:
    """Fail 时的 cache fallback（TODO #13）：回读上次成功写入的 JSON 里对应 block。

    返回带 `stale: true` 标记的旧数据（非空才算），让渲染层图不消失、但可标注数据非本期。
    """
    try:
        prev = json.loads(OUT.read_text())
    except Exception:
        return None
    block = prev.get(key) or {}
    if not (block.get("dates") and block.get("values")):
        return None
    block = dict(block)
    block["stale"] = True
    block["stale_from"] = prev.get("fetched_at")
    return block


def summarize(result):
    """Recompute derived fields after independent component acceptance."""
    cvdd_cur = (result.get('cvdd') or {}).get('current')
    mz = (result.get('mvrv_zscore') or {}).get('current')
    thresholds = result['thresholds']
    result['summary'] = {'cvdd_current': cvdd_cur, 'mvrv_z_current': mz}
    result['signals'] = {'cvdd_signal': None, 'mvrv_z_signal':
                         None if mz is None else 'bottom' if mz < thresholds['mvrv_z_bottom']
                         else 'top' if mz >= thresholds['mvrv_z_top'] else 'neutral'}


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    result: dict = {
        "fetched_at": datetime.now().isoformat(),
        "source": "woocharts.com (CVDD, Willy Woo official) + BRK raw capitalizations / local MVRV Z calculation",
        "thresholds": {
            "cvdd_bottom_band": CVDD_BOTTOM_BAND,
            "mvrv_z_bottom": MVRV_Z_BOTTOM,
            "mvrv_z_top": MVRV_Z_TOP,
        },
        "errors": [],
    }

    # --- CVDD ---
    try:
        cvdd_data = fetch_cvdd()
        result["cvdd"] = cvdd_data
        cur = cvdd_data["current"]
        print(
            f"OK fetch_bgeometrics cvdd (woocharts): {len(cvdd_data['dates'])} rows, "
            f"latest {cvdd_data['current_date']} = ${cur:,.2f}",
            file=sys.stderr,
        )
    except Exception as exc:
        result["errors"].append(f"cvdd: {exc}")
        prev = _load_previous("cvdd")
        if prev:
            result["cvdd"] = prev
            print(f"FAIL fetch_bgeometrics cvdd: {exc} → 沿用上次数据（{prev.get('stale_from')}，标 stale）", file=sys.stderr)
        else:
            result["cvdd"] = {"dates": [], "values": [], "current": None, "current_date": None}
            print(f"FAIL fetch_bgeometrics cvdd: {exc}（无上次数据可 fallback）", file=sys.stderr)

    # --- MVRV Z-Score ---
    try:
        mz_data = fetch_mvrv_zscore()
        result["mvrv_zscore"] = mz_data
        cur_z = mz_data["current"]
        print(
            f"OK fetch_bgeometrics mvrv_zscore: {len(mz_data['dates'])} rows, "
            f"latest {mz_data['current_date']} = {cur_z:.4f}",
            file=sys.stderr,
        )
    except Exception as exc:
        result["errors"].append(f"mvrv_zscore: {exc}")
        prev = _load_previous("mvrv_zscore")
        if prev:
            result["mvrv_zscore"] = prev
            print(f"FAIL fetch_bgeometrics mvrv_zscore: {exc} → 沿用上次数据（{prev.get('stale_from')}，标 stale）", file=sys.stderr)
        else:
            result["mvrv_zscore"] = {"dates": [], "values": [], "current": None, "current_date": None}
            print(f"FAIL fetch_bgeometrics mvrv_zscore: {exc}（无上次数据可 fallback）", file=sys.stderr)

    cvdd_cur = (result.get("cvdd") or {}).get("current")
    mvrv_z_cur = (result.get("mvrv_zscore") or {}).get("current")

    summarize(result)

    temporary = OUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    temporary.replace(OUT)
    err_n = len(result["errors"])
    print(
        f"OK fetch_bgeometrics done: CVDD={cvdd_cur} · MVRV-Z={mvrv_z_cur} · errors={err_n} -> {OUT}",
        file=sys.stderr,
    )
    return 0 if err_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
