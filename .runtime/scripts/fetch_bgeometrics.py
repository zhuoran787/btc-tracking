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

- MVRV Z-Score：BGeometrics REST API（保留不变）
    https://api.bgeometrics.com/v1/mvrv-zscore
  MVRV Z 是无量纲 z-score，BGeometrics 值已验证与 MacroMicro / Glassnode 吻合（~0.79 vs 0.87）。

阈值参考（用户 2026-05-21 定）：
  BTC Price / CVDD ≤ 1.03 = 大周期底部
  MVRV Z < 0   = 大周期底部信号
  MVRV Z ≥ 4.0 = 大周期顶部信号

Output: data/bgeometrics_data.json
"""
import json
import re
import sys
from datetime import datetime, timezone
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

# MVRV Z-Score REST API
MVRV_Z_URL = "https://api.bgeometrics.com/v1/mvrv-zscore"

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


def fetch_mvrv_zscore() -> dict:
    """Fetch MVRV Z-Score from BGeometrics REST API.

    2026-07-01 加固（TODO #13）：BGeometrics 免费 API 偶发 429 限流（2026-05-21/22 两次实测），
    429 时按 15s/30s 退避重试 2 次；仍失败由 main() 走上次 JSON 的 stale fallback。
    """
    import time
    last_exc: Exception | None = None
    for attempt, backoff in enumerate((0, 15, 30)):
        if backoff:
            print(f"  mvrv_zscore 429 backoff {backoff}s (retry {attempt}/2)...", file=sys.stderr)
            time.sleep(backoff)
        try:
            r = requests.get(MVRV_Z_URL, timeout=30)
            r.raise_for_status()
            break
        except requests.HTTPError as exc:
            last_exc = exc
            if exc.response is not None and exc.response.status_code == 429:
                continue  # 限流才重试
            raise
    else:
        raise last_exc  # 3 次全 429
    rows = r.json()
    out = []
    for row in rows:
        d = row.get("d")
        v = row.get("mvrvZscore")
        if d is None or v is None:
            continue
        out.append({"date": d, "value": float(v)})
    out.sort(key=lambda x: x["date"])
    if len(out) < 100:
        raise ValueError(f"too short ({len(out)} rows)")
    latest = out[-1]
    # The free tier explicitly withholds the latest seven days. Confirm this
    # with the provider's /last metadata instead of treating the lag as downtime.
    last_response = requests.get(MVRV_Z_URL + '/last', timeout=30)
    last_response.raise_for_status()
    metadata = last_response.json()
    if metadata.get('d') != latest['date'] or abs(float(metadata['mvrvZscore']) - latest['value']) > 1e-8:
        raise ValueError('MVRV history and /last disagree')
    delay_match = re.search(r'last (\d+) days', metadata.get('message', ''), re.I)
    return {
        "dates": [r["date"] for r in out],
        "values": [round(r["value"], 4) for r in out],
        "current": latest["value"],
        "current_date": latest["date"],
        "delayed": metadata.get('delayed') is True,
        "delay_days": int(delay_match.group(1)) if delay_match else None,
        "delay_message": metadata.get('message', ''),
        "source_url": MVRV_Z_URL,
    }


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
        "source": "woocharts.com (CVDD, Willy Woo official) + BGeometrics REST API (MVRV Z)",
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

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    err_n = len(result["errors"])
    print(
        f"OK fetch_bgeometrics done: CVDD={cvdd_cur} · MVRV-Z={mvrv_z_cur} · errors={err_n} -> {OUT}",
        file=sys.stderr,
    )
    return 0 if err_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
