"""
generate_report.py — Assemble all data into an HTML report via Jinja2

Reads: data/api_data.json, data/etf_flows.json, data/treasuries.json
Accepts: user inputs + WebSearch results via stdin JSON (optional)
Outputs: ai-work/btc-tracking/YYYY-MM-DD/btc-tracking-report.html
"""

import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


def _load_ppt_asset(name):
    """Return base64 data URI for tools/btc-tracking/data/ppt_assets/<name>.png, or None."""
    p = Path(__file__).parent.parent / "data" / "ppt_assets" / f"{name}.png"
    if not p.exists():
        return None
    return f"data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}"


def _load_screenshot(name):
    """Return base64 data URI for tools/btc-tracking/data/screenshots/<name>.png, or None."""
    p = Path(__file__).parent.parent / "data" / "screenshots" / f"{name}.png"
    if not p.exists():
        return None
    return f"data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}"

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
TEMPLATE_DIR = PROJECT_DIR / "templates"
WORK_ROOT = PROJECT_DIR.parent.parent  # ~/Desktop/claude trial 1/
OUTPUT_BASE = WORK_ROOT / "ai-work" / "btc-tracking"

# ── ahr999 阈值常量（九神 2018 年提出社区惯例）────────
# 改这两个数字 = 全报告同步生效（染色 / 信号 / 模板"阈值参考"格 / 数据变化文字）
AHR999_BOTTOM = 0.45  # ≤ 此值 = buy-the-dip 大周期底
AHR999_TOP = 1.0      # ≥ 此值 = over-value 大周期顶（九神原版用 1.2，本项目用 1.0）

# ── 四年周期见底预测日 ────────
# 2026-07-05 /skill-harden B3：用户填的 cycle_summary 文本是唯一权威源——
# 渲染时自动取其中第一个 YYYY-MM-DD 作为见底日（驱动倒计时 + fallback 文案）；
# 本常量只在用户未填 / 文本里解析不到日期时兜底。
CYCLE_BOTTOM_DATE = "2026-10-15"


def cycle_bottom_from_text(cycle_text):
    """从用户 cycle_summary 文本解析见底日（取第一个 YYYY-MM-DD），解析不到回退常量。"""
    m = re.search(r"\d{4}-\d{1,2}-\d{1,2}", cycle_text or "")
    return m.group(0) if m else CYCLE_BOTTOM_DATE

# ── funding 负值信号阈值（PPT 因素 6 图 1 信号触发，2026-05-21 用户定）────────
# 规则："最近 FUNDING_LOOKBACK_DAYS 天内累计**严格超过** FUNDING_NEG_DAYS_FOR_SIGNAL 天的 8h funding ≤ FUNDING_NEG_THRESHOLD"
# 即至少 (FUNDING_NEG_DAYS_FOR_SIGNAL + 1) 天 → 触发底部信号
FUNDING_NEG_THRESHOLD = -0.01     # 单位 %，rate_8h ≤ 此值算"显著负值"（数据 rate_8h 已是百分比形式）
FUNDING_LOOKBACK_DAYS = 30        # 滑动窗口长度
FUNDING_NEG_DAYS_FOR_SIGNAL = 3   # 累计天数**严格大于**此值触发（即 ≥ 4 天）


# ── Helpers ──────────────────────────────────────────────────

def fmt_num(val, decimals=0):
    if val is None:
        return "N/A"
    if abs(val) >= 1e12:
        return f"{val/1e12:,.{decimals}f}T"
    if abs(val) >= 1e9:
        return f"{val/1e9:,.{max(decimals,1)}f}B"
    if abs(val) >= 1e6:
        return f"{val/1e6:,.{max(decimals,1)}f}M"
    if abs(val) >= 1e3:
        return f"{val/1e3:,.{max(decimals,1)}f}K"
    return f"{val:,.{decimals}f}"


def fmt_usd(val):
    if val is None:
        return "N/A"
    return "$" + fmt_num(val)


def pct_color(pct):
    if pct is None:
        return "#8892a8"
    if pct < 20:
        return "#22c55e"
    if pct > 80:
        return "#ef4444"
    return "#eab308"


def signal_icon(val):
    if val is None:
        return "➡️"
    return "⬆️" if val > 0 else ("⬇️" if val < 0 else "➡️")


def row_highlight(change_7d):
    if change_7d is None:
        return ""
    if change_7d > 5:
        return "highlight-green"
    if change_7d < -5:
        return "highlight-red"
    return ""


# ── Data loading ─────────────────────────────────────────────

def load_screenshots():
    """Return dict keyed by anchor (factor section) for inline embedding."""
    import base64
    screenshot_dir = SCRIPT_DIR.parent / "data" / "screenshots"
    if not screenshot_dir.exists():
        return {}
    # (file_name, anchor, caption, source_url, note)
    # anchor = which factor section to embed in
    meta = [
        ("saylortracker_strategy.png", "dat",
         "Strategy 多年 BTC 购买时序",
         "https://www.saylortracker.com/",
         "$66.4B 储备 · 818,334 BTC · 均价 $75,537 · +7.42% PnL"),
        ("bitcointreasuries_chart.png", "dat",
         "全 DAT 公司详细表 + 每家 BTC 累积 sparkline",
         "https://bitcointreasuries.net/",
         "Strategy 818K / XXI 43.5K / Metaplanet 40K / MARA 38.7K 等头部全名单及 cost basis / mNAV / 占 21M 比例"),
        ("bitcoinquant_treasuries.png", "dat",
         "DAT 多维分析（BitcoinQuant.co）",
         "https://bitcoinquant.co/treasuries",
         "48 家公司全表 + 财务指标"),
        ("coinglass_btc_oi.png", "oi",
         "全市场 BTC OI（按交易所拆分）",
         "https://www.coinglass.com/BitcoinOpenInterest",
         "CME / Binance / OKX / Bybit / KuCoin 等交易所 OI + 24h/30d 变化（你原稿要的 Coinglass OI）"),
        ("coinglass_btc_funding.png", "funding",
         "OI 加权 Funding Rate",
         "https://www.coinglass.com/FundingRate",
         "用户原稿要的 #5 数据 1：BTC OI-weighted funding = 0.0028% · highest/lowest 套利窗口"),
    ]
    by_anchor: dict = {"dat": [], "oi": [], "funding": []}
    for fname, anchor, caption, url, note in meta:
        p = screenshot_dir / fname
        if not p.exists():
            continue
        b64 = base64.b64encode(p.read_bytes()).decode()
        by_anchor.setdefault(anchor, []).append({
            "name": fname,
            "data_uri": f"data:image/png;base64,{b64}",
            "caption": caption,
            "url": url,
            "note": note,
            "size_kb": p.stat().st_size // 1024,
        })
    return by_anchor


def load_data():
    data = {}
    for name in (
        "api_data", "etf_flows", "treasuries",
        # Session 3 additions
        "brk_data", "derivatives_data",
        "mnav_data", "mnav_history",
        "dat_data", "dat_history",
        "tether_data",
        "coinshares_data",
        "policy_data",
        "whale_tweets",
        # PPT 2026-05 加入：fear & greed + ahr999 自合成 + Exchange Reserve（Coinglass scrape）
        "fear_greed",
        "ahr999",
        "exchange_reserve",
        "coinmetrics_data",  # Coin Metrics Community: SplyExNtv 17 年 daily
        "bgeometrics_data",  # CVDD + MVRV Z-Score 自由 API（情绪指标图 4/5）
        "strategy_data",     # Strategy 官方购买史 + 自算 mNAV（2026-07-02 加，TODO #2/#7）
    ):
        path = DATA_DIR / f"{name}.json"
        if path.exists():
            try:
                with open(path) as f:
                    data[name] = json.load(f)
            except Exception as e:
                print(f"WARNING: failed to load {path}: {e}")
                data[name] = {}
        else:
            data[name] = {}
    return data


def load_extra_input():
    """读 stdin extra JSON，并自动合并 data/opinions_input.json（KOL SSOT）。

    Single Source of Truth：KOL 数据存 data/opinions_input.json，
    老报告（generate_report）和独立报告（render_opinions_only）都读同一份，
    保证 SECTION C 和 SECTION A 总结表 KOL 行不会出现新老不一致。
    若 stdin extra 也含 opinions 字段，**SSOT 覆盖** extra.opinions。
    """
    extra = {}
    if not sys.stdin.isatty():
        try:
            extra = json.load(sys.stdin)
        except Exception:
            pass

    ssot_path = DATA_DIR / "opinions_input.json"
    if ssot_path.exists():
        try:
            with open(ssot_path) as f:
                ssot = json.load(f)
            ssot_opinions = {}
            for cat in ssot.get("categories", []):
                ssot_opinions[cat["name"]] = cat.get("people", [])
            if ssot_opinions:
                extra["opinions"] = ssot_opinions
                # v3.5 2026-05-21: 强制 SSOT 路径——AI 不许通过 stdin 传 opinion_one_liner
                # 先抹掉 AI 直传，再从 SSOT 设；SSOT 没有就让下游 helper 计算（_opinions_one_liner）
                extra.pop("opinion_one_liner", None)
                if ssot.get("one_liner"):
                    extra["opinion_one_liner"] = ssot["one_liner"]
                print(
                    f"INFO: Loaded SSOT opinions from {ssot_path.name} "
                    f"({sum(len(p) for p in ssot_opinions.values())} cards across {len(ssot_opinions)} groups)",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"WARN: SSOT load failed: {e}", file=sys.stderr)

    # User-inputs cache merge：5 个 SECTION A 用户填字段 fallback to cache + freshness tagging
    # 跑报告前 Claude 应逐项问用户'要改吗'，改了写入 extra.user_inputs.X 触发 fresh 标记。
    ui_cache_path = DATA_DIR / "user_inputs_cache.json"
    extra.setdefault("user_inputs", {})
    extra.setdefault("_user_inputs_freshness", {})
    if ui_cache_path.exists():
        try:
            with open(ui_cache_path) as f:
                ui_cache = json.load(f)
            today = datetime.now().strftime("%Y-%m-%d")
            for key in ("macro_summary", "cycle_summary", "dat_outflow_bullish", "trump_clearance_bullish", "miner_selloff_note"):
                if extra["user_inputs"].get(key):
                    extra["_user_inputs_freshness"][key] = {"status": "fresh", "date": today}
                else:
                    entry = ui_cache.get(key) or {}
                    if entry.get("value"):
                        extra["user_inputs"][key] = entry["value"]
                        extra["_user_inputs_freshness"][key] = {
                            "status": "cached",
                            "date": entry.get("_last_updated", "—"),
                            "seed": entry.get("_status") == "seed",
                        }
        except Exception as e:
            print(f"WARN: user_inputs cache load failed: {e}", file=sys.stderr)

    # WebSearch cache merge：fields not provided by stdin extra fallback to cache
    # cache 用于"上次 WebSearch 结果"，模板渲染时标 "未本期 WebSearch · 上次 YYYY-MM-DD"
    cache_path = DATA_DIR / "websearch_cache.json"
    extra.setdefault("_websearch_freshness", {})
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cache = json.load(f)
            # 字段映射：(extra_key, cache_key, items/text)
            cache_fields = [
                ("dat_announcements", "dat_announcements", "items"),
                # whale_news 已合并到 SSOT 资金面分析组 5 卡（不再独立 cache 渲染）
                ("verify_1_strategic_reserve", "verify_1_strategic_reserve", "text"),
                ("verify_2_trump_clearance", "verify_2_trump_clearance", "text"),
                ("verify_3_institutional_entry", "verify_3_institutional_entry", "text"),
                ("verify_4_clarity_passage_probability", "verify_4_clarity_passage_probability", "text"),
                # tldr = Phase 2 写的 ≤100 字真提炼（2026-07-04 用户拍板），SECTION A 行 7 一句话优先用它拼装
                ("verify_1_strategic_reserve_tldr", "verify_1_strategic_reserve", "tldr"),
                ("verify_2_trump_clearance_tldr", "verify_2_trump_clearance", "tldr"),
                ("verify_3_institutional_entry_tldr", "verify_3_institutional_entry", "tldr"),
                ("verify_4_clarity_passage_probability_tldr", "verify_4_clarity_passage_probability", "tldr"),
            ]
            loaded = []
            for extra_key, cache_key, value_field in cache_fields:
                if extra.get(extra_key):
                    extra["_websearch_freshness"][extra_key] = {"status": "fresh", "date": datetime.now().strftime("%Y-%m-%d")}
                    continue
                cache_entry = cache.get(cache_key) or {}
                value = cache_entry.get(value_field)
                if value:
                    extra[extra_key] = value
                    extra["_websearch_freshness"][extra_key] = {
                        "status": "cached",
                        "date": cache_entry.get("_last_search_date", "—"),
                        "seed": cache_entry.get("_status") == "seed",
                    }
                    loaded.append(extra_key)
            if loaded:
                print(f"INFO: Loaded WebSearch cache for {loaded}", file=sys.stderr)
        except Exception as e:
            print(f"WARN: WebSearch cache load failed: {e}", file=sys.stderr)
    return extra


def save_user_inputs_cache(extra):
    """跑过 fresh 用户输入的 4 项写回 cache，覆盖 seed/上一版。"""
    cache_path = DATA_DIR / "user_inputs_cache.json"
    if not cache_path.exists():
        return
    try:
        with open(cache_path) as f:
            cache = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        freshness = extra.get("_user_inputs_freshness", {})
        user_inputs = extra.get("user_inputs", {})
        updated = []
        for key in ("macro_summary", "cycle_summary", "dat_outflow_bullish", "trump_clearance_bullish"):
            if freshness.get(key, {}).get("status") == "fresh" and user_inputs.get(key):
                cache.setdefault(key, {})["value"] = user_inputs[key]
                cache[key]["_last_updated"] = today
                cache[key]["_status"] = "fresh"
                updated.append(key)
        if updated:
            with open(cache_path, "w") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            print(f"INFO: Saved fresh user inputs to cache for {updated}", file=sys.stderr)
    except Exception as e:
        print(f"WARN: user_inputs cache save failed: {e}", file=sys.stderr)


def save_websearch_cache(extra):
    """跑过 fresh WebSearch 的字段写回 cache，覆盖 seed/老内容。"""
    cache_path = DATA_DIR / "websearch_cache.json"
    if not cache_path.exists():
        return
    try:
        with open(cache_path) as f:
            cache = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        freshness = extra.get("_websearch_freshness", {})
        updated = []
        field_map = {
            "dat_announcements": ("dat_announcements", "items"),
            # whale_news 已合并到 SSOT 资金面分析组 5 卡（不再 cache）
            "verify_1_strategic_reserve": ("verify_1_strategic_reserve", "text"),
            "verify_2_trump_clearance": ("verify_2_trump_clearance", "text"),
            "verify_3_institutional_entry": ("verify_3_institutional_entry", "text"),
            "verify_4_clarity_passage_probability": ("verify_4_clarity_passage_probability", "text"),
        }
        for extra_key, (cache_key, value_field) in field_map.items():
            if freshness.get(extra_key, {}).get("status") == "fresh" and extra.get(extra_key):
                cache.setdefault(cache_key, {})[value_field] = extra[extra_key]
                cache[cache_key]["_last_search_date"] = today
                cache[cache_key]["_status"] = "fresh"
                updated.append(extra_key)
        if updated:
            with open(cache_path, "w") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            print(f"INFO: Saved fresh WebSearch results to cache for {updated}", file=sys.stderr)
    except Exception as e:
        print(f"WARN: WebSearch cache save failed: {e}", file=sys.stderr)


# ── Factor Dashboard ─────────────────────────────────────────

def build_factor_rows(data, extra):
    api = data.get("api_data", {})
    etf = data.get("etf_flows", {})
    tres = data.get("treasuries", {})
    brk = data.get("brk_data", {})
    deriv = data.get("derivatives_data", {})
    dat = data.get("dat_data", {})
    user = extra.get("user_inputs", {})
    rows = []

    # 0 Macro — two-sentence pattern per user's original spec:
    #  · macro_summary       = user's qualitative judgment on financial conditions
    #  · macro_interpretation = AI's interpretation of BTC-Gold/SOX correlations + price action
    # Falls back to mechanical correlation numbers ONLY if both are absent (cron mode).
    btc = api.get("btc_price", {})
    corr = api.get("correlations", {})
    c7 = btc.get("change_7d")
    macro_user = user.get("macro_summary")
    macro_ai = user.get("macro_interpretation")
    if macro_user and macro_ai:
        macro_status = f"{macro_user} | {macro_ai}"
    elif macro_user:
        macro_status = f"{macro_user} | (AI 解读待生成 · BTC-Gold {corr.get('btc_gold_3m', 0):.2f} / BTC-SOX {corr.get('btc_sox_3m', 0):.2f})"
    else:
        macro_status = f"BTC ${btc.get('current', 0):,.0f} · 相关性 Gold {corr.get('btc_gold_3m', 0):.2f} / SOX {corr.get('btc_sox_3m', 0):.2f}"
    rows.append(dict(
        name="宏观环境", status=macro_status,
        change_7d=c7, change_30d=btc.get("change_30d"), change_90d=btc.get("change_90d"),
        percentile=None, pct_color="#8892a8",
        signal=signal_icon(c7), highlight=row_highlight(c7),
        rationale=f"BTC-Gold 3m returns: {corr.get('btc_gold_3m', 0):.2f} · BTC-SOX 3m returns: {corr.get('btc_sox_3m', 0):.2f}",
    ))

    # 1 Cycle — PPT 明确要求不放 detailed-data；总结表 SECTION A 保留四年周期行（用户输入），此处不再生成
    # （删除原 rows.append cycle 块）

    # 2 DAT — prefer CoinGecko dat_data (174 companies + cost basis) over old tres
    dat_total = dat.get("total_holdings")
    dat_dom = dat.get("market_cap_dominance_pct")
    dat_count = dat.get("company_count")
    if dat_total:
        dat_status_default = f"公链 {fmt_num(dat_total)} BTC · {dat_count} 公司 · 市值占比 {dat_dom}%"
    else:
        dat_status_default = f"公链 {fmt_num(tres.get('totals', {}).get('public_btc', 0))} BTC"
    rows.append(dict(
        name="DAT公司", status=user.get("dat_summary", dat_status_default),
        change_7d=None, change_30d=None, change_90d=None,
        percentile=None, pct_color="#8892a8",
        signal="➡️", highlight="",
        rationale=_top_company_line_v2(dat) or _top_company_line(tres),
    ))

    # 3 ETF — 7d/30d are dollar sums, not percentages; use special display
    s = etf.get("summary", {})
    s7 = s.get("sum_7d_m")
    s30 = s.get("sum_30d_m")
    rows.append(dict(
        name="ETF资金流", status=f"7日 ${s7 or 0:,.0f}M · 30日 ${s30 or 0:,.0f}M",
        change_7d=None, change_30d=None, change_90d=None,
        percentile=None, pct_color="#8892a8",
        signal=signal_icon(s7), highlight="highlight-green" if s7 and s7 > 500 else ("highlight-red" if s7 and s7 < -500 else ""),
        rationale=f"最新 {s.get('latest_date', '')} ${s.get('latest_flow_m', 0):,.0f}M · 正/负 {s.get('positive_days_30d', 0)}/{s.get('negative_days_30d', 0)}天",
        etf_7d=s7, etf_30d=s30,
    ))

    # 4 Whales — BRK 鲸鱼供应 (>=1k BTC 地址) 7d/30d delta
    accum = brk.get("accumulation", {}) or {}
    whale_cur = accum.get("whale_current")
    whale_7d = accum.get("whale_7d_change")
    whale_30d = accum.get("whale_30d_change")
    if whale_cur:
        whale_status_default = (
            f"鲸鱼供应 (>=1k BTC) {fmt_num(whale_cur)} · "
            f"7d {whale_7d:+,.0f} · 30d {whale_30d:+,.0f}"
        )
        whale_signal = signal_icon(whale_30d)
    else:
        whale_status_default = "—"
        whale_signal = "➡️"
    rows.append(dict(
        name="鲸鱼动向", status=user.get("whale_summary", whale_status_default),
        change_7d=None, change_30d=None, change_90d=None,
        percentile=None, pct_color="#8892a8",
        signal=whale_signal, highlight="",
        # AI override (extra.whale_rationale) 已删除 2026-05-21——强制走 helper 计算
        rationale=(
            f"过去 30 天鲸鱼地址（>=1k BTC）供应{('增加' if whale_30d and whale_30d > 0 else '减少' if whale_30d and whale_30d < 0 else '基本持平')} {abs(whale_30d):,.0f} BTC" if whale_30d is not None else "WebSearch 补充"
        ),
    ))

    # 5 情绪指标 — funding rate + fear & greed + ahr999（按 PPT 因素 6 三图合并）
    f = api.get("funding", {})
    fr = f.get("current_rate_pct", 0)
    fp = f.get("percentile_730d")  # 2026-07-04 用户拍板：90d 基期含暴跌段会虚高（+0.00875 排 80 分位），改用 730d
    fr_hist = f.get("history_8h", [])
    fr_7d_avg = _avg_funding(fr_hist, 7)
    fr_30d_avg = _avg_funding(fr_hist, 30)
    fg = data.get("fear_greed", {}) or {}
    fg_cur = fg.get("current")
    ahr = data.get("ahr999", {}) or {}
    ahr_cur = ahr.get("current")
    sentiment_pieces = [f"funding {fr:+.4f}%"]
    if fg_cur is not None:
        sentiment_pieces.append(f"fear&greed {fg_cur} ({fg.get('classification', '—')})")
    if ahr_cur is not None:
        sentiment_pieces.append(f"ahr999 {ahr_cur:.3f}")
    sentiment_status = " · ".join(sentiment_pieces)
    # 信号判断：funding 近 30d 连续负 >=3d / fear<=25 / greed>=75 / ahr999 阈值见 AHR999_BOTTOM/TOP
    sentiment_signal = "➡️"
    if (fg_cur is not None and fg_cur <= 25) or (ahr_cur is not None and ahr_cur <= AHR999_BOTTOM):
        sentiment_signal = "⬇️ 底部信号"
    elif (fg_cur is not None and fg_cur >= 76) or (ahr_cur is not None and ahr_cur >= AHR999_TOP):
        sentiment_signal = "⬆️ 顶部信号"
    rows.append(dict(
        name="情绪指标", status=sentiment_status,
        change_7d=fr_7d_avg, change_30d=fr_30d_avg, change_90d=None,
        percentile=fp, pct_color=pct_color(fp),
        signal=sentiment_signal, highlight="",
        rationale=_funding_streak(f),
        fmt="funding",
    ))

    # 6 OI — 独立成因素 7（PPT 拆出来）
    agg = deriv.get("coingecko_aggregate", {}) or {}
    market_oi = agg.get("total_oi_usd")
    market_fr = agg.get("oi_weighted_funding_pct")
    if market_oi:
        oi_status = (
            f"全市场 OI ${market_oi/1e9:.1f}B · OI 加权资金费率 {market_fr:+.4f}%"
            if market_fr is not None
            else f"全市场 OI ${market_oi/1e9:.1f}B"
        )
    else:
        oi_status = f"OI ${f.get('current_oi_usd', 0)/1e9:.1f}B"
    rows.append(dict(
        name="永续合约 OI", status=oi_status,
        change_7d=None, change_30d=None, change_90d=None,
        percentile=None, pct_color="#8892a8",
        signal="➡️", highlight="",
        rationale=f"OKX BTC-USDT-SWAP 100d 历史 + CoinGecko {agg.get('num_exchanges', 0)} 家聚合",
    ))

    # 6 Stablecoin
    st = api.get("stablecoins", {})
    sc7 = st.get("change_7d")
    rows.append(dict(
        name="稳定币供应", status=f"总量 {fmt_usd(st.get('total_mcap', 0))}",
        change_7d=sc7, change_30d=st.get("change_30d"), change_90d=None,
        percentile=None, pct_color="#8892a8",
        signal=signal_icon(sc7), highlight="",
        rationale=f"USDT {fmt_usd(st.get('usdt_mcap', 0))} + USDC {fmt_usd(st.get('usdc_mcap', 0))}",
    ))

    # 7 Policy — Factor 9 (strategic reserve) / 10 (新机构动态) / 11 (Clarity Act)
    # AI-curated strings via extra take priority. Auto-fallback from policy_data.json.
    f9 = extra.get("factor_9_reserve")
    f10 = extra.get("factor_10_institution")
    f11 = extra.get("factor_11_clarity")
    if not any([f9, f10, f11]):
        # auto-derive from policy_data.json
        policy = data.get("policy_data", {})
        f9_hits = [h for h in policy.get("factor_9_strategic_reserve", []) if "error" not in h]
        f10_hits = [h for h in policy.get("factor_10_institution", []) if "error" not in h]
        clarity = policy.get("factor_11_clarity_act") or {}
        clarity_latest = clarity.get("latest_action") or {}
        if f9_hits:
            f9 = f"{len(f9_hits)} 条战略储备相关行政令/EO 命中"
        if f10_hits:
            f10 = f"近 30 天 SEC EDGAR {len(f10_hits)} 条 ETF/数字货币 filings"
        if clarity_latest.get("date"):
            f11 = f"Clarity Act 最新动作 {clarity_latest.get('date')}: {(clarity_latest.get('text') or '')[:60]}"
    policy_parts = [p for p in [f9, f10, f11] if p]
    policy_status = " | ".join(policy_parts) if policy_parts else user.get("legislation_summary", "无新变化")
    rows.append(dict(
        name="政策/机构", status=policy_status,
        change_7d=None, change_30d=None, change_90d=None,
        percentile=None, pct_color="#8892a8",
        signal="⚡" if policy_parts else "➡️",
        highlight="highlight-green" if policy_parts else "",
        rationale=" · ".join(policy_parts) if policy_parts else "本周无重大变化",
    ))

    # 8 KOL Opinions — bull/bear ratio
    opinions = extra.get("opinions", {})
    bull_count = len(opinions.get("币圈老鸟，一般倾向看多") or opinions.get("看多 KOL", []))
    bear_count = len(opinions.get("币圈的空头") or opinions.get("看空 KOL", []))
    fund_count = len(opinions.get("资金面分析", []))
    hedge_count = len(opinions.get("对冲基金，一般中性偏多") or opinions.get("对冲基金", []))
    total_voices = bull_count + bear_count + fund_count + hedge_count
    if total_voices > 0:
        bull_pct = round((bull_count / total_voices) * 100)
        kol_status = f"多{bull_count} / 空{bear_count} / 基金{hedge_count} / 资金面{fund_count}"
        rows.append(dict(
            name="KOL观点", status=kol_status,
            change_7d=None, change_30d=None, change_90d=None,
            percentile=bull_pct, pct_color=pct_color(bull_pct),
            signal="⬆️" if bull_pct > 60 else ("⬇️" if bull_pct < 40 else "➡️"),
            highlight="",
            rationale=f"多头占比 {bull_pct}%（{bull_count}/{total_voices}）",
        ))

    return rows


def _top_company_line_v2(dat):
    """Summarize the four dynamically tracked DAT companies."""
    cb = dat.get("cost_basis_summary", []) or []
    parts = []
    targets = ["Strategy", "Twenty One Capital", "Metaplanet", "MARA Holdings"]
    for c in cb:
        if c.get("name") in targets:
            h = c.get("holdings")
            avg = c.get("avg_cost_basis_usd")
            avg_native = c.get("avg_cost_basis_native")
            pnl = c.get("pnl_pct")
            if h:
                short = c["name"].split(" ")[0]
                tag = f"{short}: {fmt_num(h)}"
                if avg:
                    tag += f" @{avg:,.0f}"
                elif avg_native and c.get("cost_currency") == "JPY":
                    tag += f" @¥{avg_native:,.0f}"
                if pnl is not None:
                    tag += f" ({pnl:+.0f}%)"
                parts.append(tag)
    return " · ".join(parts) if parts else None


def _top_company_line(tres):
    kc = tres.get("key_companies", {})
    parts = []
    for name in ["Strategy", "Metaplanet Inc", "MARA Holdings, Inc"]:
        co = kc.get(name, {})
        h = co.get("btc_holdings")
        if h:
            short = name.split(" ")[0]
            parts.append(f"{short}: {fmt_num(h)}")
    return " · ".join(parts) if parts else "—"


def _fg_color(value):
    """Fear & greed 区间染色（PPT 因素 6 图 2）。"""
    if value is None:
        return "var(--text)"
    if value <= 25:
        return "#b91c1c"  # extreme fear → 红（底部信号）
    if value <= 45:
        return "#a16207"  # fear → 棕黄
    if value <= 55:
        return "#525252"  # neutral → 灰
    if value <= 75:
        return "#15803d"  # greed → 绿
    return "#b91c1c"  # extreme greed → 红（顶部信号）


def _ahr999_color(value):
    """ahr999 阈值染色（buy-the-dip AHR999_BOTTOM / over-value AHR999_TOP）。"""
    if value is None:
        return "var(--text)"
    if value <= AHR999_BOTTOM:
        return "#15803d"  # 买点 → 绿
    if value >= AHR999_TOP:
        return "#b91c1c"  # 卖点 → 红
    return "#a16207"  # 中性 → 棕黄


def _cvdd_color(price, cvdd):
    """CVDD 阈值染色（BTC Price / CVDD ≤ 1.03 = 大周期底，用户 2026-05-21 定）。"""
    if price is None or cvdd is None or cvdd <= 0:
        return "var(--text)"
    ratio = price / cvdd
    if ratio <= 1.03:
        return "#15803d"  # 逼近底部 → 绿
    if ratio >= 3.0:
        return "#a16207"  # 远离底部 → 棕黄（CVDD 只指底）
    return "var(--text)"


def _mvrv_z_color(value):
    """MVRV Z-Score 阈值染色（<0 底 / ≥4.0 顶，用户 2026-05-21 定）。"""
    if value is None:
        return "var(--text)"
    if value < 0:
        return "#15803d"  # 底部 → 绿
    if value >= 4.0:
        return "#b91c1c"  # 顶部 → 红
    return "#a16207"  # 中性 → 棕黄


def _funding_low_days_in_window(history_8h, threshold=FUNDING_NEG_THRESHOLD, lookback_days=FUNDING_LOOKBACK_DAYS):
    """统计 history_8h 最近 lookback_days 天里有多少天**至少 1 个 8h 窗口** ≤ threshold。

    基准日 = history_8h 最后一条的日期（不是系统当前日期，避免数据新鲜度滞后导致误判）。
    返回 int 天数。
    """
    if not history_8h:
        return 0
    from datetime import datetime, timedelta
    from collections import defaultdict
    try:
        last_date = datetime.fromisoformat(history_8h[-1]["date"].split(" ")[0]).date()
    except Exception:
        return 0
    cutoff = last_date - timedelta(days=lookback_days - 1)
    by_date = defaultdict(list)
    for item in history_8h:
        try:
            d = datetime.fromisoformat(item["date"].split(" ")[0]).date()
        except Exception:
            continue
        if d >= cutoff:
            by_date[d].append(item.get("rate_8h", 0))
    return sum(1 for rates in by_date.values() if any(r <= threshold for r in rates))


def _sentiment_qualitative(fg, ahr, funding, cvdd_state=None, mvrv_z=None):
    """SECTION A 一句话总结 + 因素 6 一句话总结 共用。定性结论 only，无数字。
    PPT slide 11 格式：'[X] 信号指示 [Y]'；无信号时直接 '无明显顶/底信号'。

    cvdd_state: dict {"price_cur": float, "cvdd_cur": float} 或 None
    mvrv_z: float MVRV Z-Score 当前值 或 None
    """
    parts = []
    history_8h = (funding or {}).get("history_8h", []) or []
    funding_low_days = _funding_low_days_in_window(history_8h)

    if fg is not None:
        if fg <= 25:
            parts.append("⬇️ 恐惧贪婪指数 extreme fear → 小周期底部信号")
        elif fg >= 76:
            parts.append("⬆️ 恐惧贪婪指数 extreme greed → 小周期顶部信号")
    if ahr is not None:
        if ahr <= AHR999_BOTTOM:
            parts.append("⬇️ ahr999 进入 buy-the-dip → 大周期底部信号")
        elif ahr >= AHR999_TOP:
            parts.append("⬆️ ahr999 进入 over-value → 大周期顶部信号")
    if cvdd_state:
        p = cvdd_state.get("price_cur")
        c = cvdd_state.get("cvdd_cur")
        if p is not None and c is not None and c > 0 and p / c <= 1.03:
            parts.append("⬇️ BTC Price / CVDD ≤ 1.03 → 大周期底部信号")
    if mvrv_z is not None:
        if mvrv_z < 0:
            parts.append("⬇️ MVRV Z-Score < 0 → 大周期底部信号")
        elif mvrv_z >= 4.0:
            parts.append("⬆️ MVRV Z-Score ≥ 4 → 大周期顶部信号")
    if funding_low_days > FUNDING_NEG_DAYS_FOR_SIGNAL:
        parts.append(f"⬇️ funding 近 {FUNDING_LOOKBACK_DAYS}d 累计 {funding_low_days} 天 ≤ {FUNDING_NEG_THRESHOLD:.2f}% → 小周期底部信号")

    return "；".join(parts) if parts else "无明显顶/底信号"


def _sentiment_numeric(fg, ahr, funding, cvdd_state=None, mvrv_z=None):
    """SECTION A 数据变化 列：funding / fear&greed / ahr999 / CVDD / MVRV Z 五图当前数值。"""
    fr_pct = (funding or {}).get("current_rate_pct", 0)
    history_8h = (funding or {}).get("history_8h", []) or []
    funding_low_days = _funding_low_days_in_window(history_8h)
    fg_zone = ""
    if fg is not None:
        if fg <= 25:
            fg_zone = "extreme fear"
        elif fg <= 45:
            fg_zone = "恐惧"
        elif fg <= 55:
            fg_zone = "中性"
        elif fg <= 75:
            fg_zone = "贪婪"
        else:
            fg_zone = "extreme greed"
    pieces = [
        f"funding {fr_pct:+.4f}%（近 {FUNDING_LOOKBACK_DAYS}d 累计 {funding_low_days} 天 ≤ {FUNDING_NEG_THRESHOLD:.2f}%）",
        f"恐惧贪婪 {fg if fg is not None else '—'}{'（' + fg_zone + '）' if fg_zone else ''}",
        (
            f"ahr999 {ahr:.3f}（距 buy-the-dip {AHR999_BOTTOM} {ahr - AHR999_BOTTOM:+.3f}）"
            if ahr is not None
            else "ahr999 —"
        ),
    ]
    if cvdd_state and cvdd_state.get("cvdd_cur") is not None and cvdd_state.get("price_cur") is not None:
        p = cvdd_state["price_cur"]
        c = cvdd_state["cvdd_cur"]
        ratio = p / c if c else None
        pieces.append(
            f"CVDD ${c:,.0f}（BTC ${p:,.0f}，price/CVDD {ratio:.2f}x）"
            if ratio is not None
            else f"CVDD ${c:,.0f}"
        )
    if mvrv_z is not None:
        zone = "中性" if 0 <= mvrv_z < 4.0 else ("顶部红线" if mvrv_z >= 4.0 else "底部红线")
        pieces.append(f"MVRV Z {mvrv_z:.2f}（{zone}）")
    return "；".join(pieces)


def _sentiment_signal_text(fg, ahr, funding, cvdd_state=None, mvrv_z=None):
    """Backward-compat wrapper - 因素 6 section 一句话总结 = 定性 only（铁律 7）。"""
    return _sentiment_qualitative(fg, ahr, funding, cvdd_state, mvrv_z)


def _cvdd_state_from_data(data):
    """从 bgeometrics_data + ahr999 价格序列提取 {price_cur, cvdd_cur}。
    用 CVDD 最新日期匹配 ahr999.btc_prices 同日价格（CVDD 自身不带价格）。"""
    bg = data.get("bgeometrics_data") or {}
    cvdd = (bg.get("cvdd") or {})
    cvdd_cur = cvdd.get("current")
    cvdd_date = cvdd.get("current_date")
    if cvdd_cur is None:
        return None
    ahr = data.get("ahr999") or {}
    ahr_dates = ahr.get("dates") or []
    ahr_prices = ahr.get("btc_prices") or []
    price_by_date = {ahr_dates[i]: ahr_prices[i] for i in range(min(len(ahr_dates), len(ahr_prices)))}
    price_cur = price_by_date.get(cvdd_date)
    if price_cur is None and ahr_prices:
        price_cur = ahr_prices[-1]  # 兜底：用 ahr999 最新价
    return {"price_cur": price_cur, "cvdd_cur": cvdd_cur}


def _build_cvdd_chart(data):
    """CVDD vs BTC 价格双轴图 context（同 ahr999 chart 模式）。"""
    bg = data.get("bgeometrics_data") or {}
    cvdd = (bg.get("cvdd") or {})
    cvdd_dates = cvdd.get("dates") or []
    cvdd_values = cvdd.get("values") or []
    cur = cvdd.get("current")

    ahr = data.get("ahr999") or {}
    ahr_dates = ahr.get("dates") or []
    ahr_prices = ahr.get("btc_prices") or []
    price_by_date = {ahr_dates[i]: ahr_prices[i] for i in range(min(len(ahr_dates), len(ahr_prices)))}
    cvdd_price_aligned = []
    for d in cvdd_dates:
        v = price_by_date.get(d)
        cvdd_price_aligned.append(v if v is not None else None)

    state = _cvdd_state_from_data(data)
    ratio = None
    if state and state.get("cvdd_cur") and state.get("price_cur"):
        try:
            ratio = state["price_cur"] / state["cvdd_cur"]
        except ZeroDivisionError:
            ratio = None

    return {
        "cvdd_current": cur,
        "cvdd_as_of": cvdd.get('current_date'),
        "cvdd_stale": cvdd.get('stale', False),
        "cvdd_current_price": state.get("price_cur") if state else None,
        "cvdd_price_ratio": ratio,  # price/CVDD（≤1.05 = 逼近底部）
        "cvdd_color": _cvdd_color(state.get("price_cur") if state else None, cur),
        "cvdd_chart_labels": json.dumps(cvdd_dates),
        "cvdd_chart_values": json.dumps(cvdd_values),
        "cvdd_price_values": json.dumps(cvdd_price_aligned),
    }


def _build_mvrv_z_chart(data):
    """MVRV Z-Score chart context（本项目阈值：<0 底 / ≥4.0 顶）。"""
    bg = data.get("bgeometrics_data") or {}
    mz = (bg.get("mvrv_zscore") or {})
    cur = mz.get("current")
    return {
        "mvrv_z_current": cur,
        "mvrv_z_as_of": mz.get('current_date'),
        "mvrv_z_delayed": mz.get('delayed', False),
        "mvrv_z_delay_days": mz.get('delay_days'),
        "mvrv_z_stale": mz.get('stale', False),
        "mvrv_z_color": _mvrv_z_color(cur),
        "mvrv_z_chart_labels": json.dumps(mz.get("dates", [])),
        "mvrv_z_chart_values": json.dumps(mz.get("values", [])),
        "mvrv_z_threshold_bottom": 0,
        "mvrv_z_threshold_top": 4.0,
    }


def _row_sentiment(one_liner):
    """读 SECTION A 一句话总结，识别对币价方向影响：
       - 'positive' = 明显对币价正向（→ 行底色浅绿）
       - 'negative' = 明显对币价负向（→ 行底色浅红）
       - 'neutral' = 无明确方向（不染色）
       weighted scoring: reversal 关键词 weight 3，强信号 weight 2，弱信号 weight 1。
       reversal 词通常描述"近期发生的转向"，应当主导整体判断。"""
    if not one_liner:
        return "neutral"
    t = one_liner
    # 负向 weighted
    neg_weighted = [
        # reversal / 近期转向（weight 3）— 主导
        ("转为净流出", 3), ("转空", 3), ("转跌", 3), ("由正转负", 3),
        ("再转净流出", 3), ("又转为净流出", 3),
        # 强信号（weight 2）
        ("大笔卖出", 2), ("明显卖出", 2), ("明显资金外流", 2), ("持续大幅净流出", 2),
        ("顶部信号", 2), ("extreme greed", 2), ("over-value", 2),
        ("流动性风险显现", 2), ("mNAV 跌", 2), ("mNAV 已跌", 2),
        # 普通负向（weight 1）
        ("净流出", 1), ("稀释", 1), ("承压", 1),
        ("看空", 1), ("悲观", 1), ("跌至", 1), ("崩盘", 1), ("下跌", 1),
        ("卖了 28%", 1),
        ("经济处在向滞涨", 1), ("向滞涨", 1), ("过热", 1),
    ]
    # 正向 weighted
    pos_weighted = [
        # reversal / 近期转向（weight 3）
        ("转为净流入", 3), ("转多", 3), ("转涨", 3), ("由负转正", 3),
        ("再转净流入", 3), ("又转为净流入", 3),
        # 强信号（weight 2）
        ("大笔买入", 2), ("明显资金净流入", 2), ("持续大幅净流入", 2),
        ("底部信号", 2), ("extreme fear", 2), ("buy-the-dip", 2),
        ("立法预期目前是乐观", 2), ("Clarity Act 已通过", 2),
        ("Clarity Act 签署", 2), ("Clarity Act 通过", 2),
        # 普通正向（weight 1）
        ("净流入", 1), ("温和净流入", 1),
        ("利好", 1), ("乐观", 1),
        ("看多", 1), ("目标价", 1),
        ("持币存量回到历史新高", 1), ("加速购买", 1), ("停止比特币卖出", 1),
        ("持续降低、已降", 1),
        ("重新回到正相关、比特币强于", 1),
    ]
    neg_score = sum(w for k, w in neg_weighted if k in t)
    pos_score = sum(w for k, w in pos_weighted if k in t)
    if pos_score > neg_score and pos_score >= 2:
        return "positive"
    if neg_score > pos_score and neg_score >= 2:
        return "negative"
    return "neutral"


def _macro_correlation_one_liner_ai(api):
    """PPT 1.2 / slide 2 → SECTION A 宏观行第 2 段 + slide 2 caption 共用。

    PPT 范例："比特币与黄金近6个月是负相关、4月以来转为正相关。与AI资产近3个月明显正相关"
    PPT 规则："如果相关性强就写、相关性弱就不写"
    阈值：|c| < 0.2 → 跳过该 period 不写；0.2 ≤ |c| < 0.5 → "正/负相关"；|c| ≥ 0.5 → "明显正/负相关"
    """
    corr = (api or {}).get("correlations", {}) or {}

    def _describe(c, period_label):
        if c is None or abs(c) <= 0.3:
            return ""  # 相关性 |corr| > 0.3 才写
        strength = "明显" if abs(c) >= 0.5 else ""
        direction = "正相关" if c > 0 else "负相关"
        return f"近 {period_label}{strength}{direction}"

    g3 = corr.get("btc_gold_3m")
    g6 = corr.get("btc_gold_6m")
    s3 = corr.get("btc_sox_3m")
    s6 = corr.get("btc_sox_6m")

    gold_parts = [p for p in (_describe(g6, "6 个月"), _describe(g3, "3 个月")) if p]
    ai_parts = [p for p in (_describe(s6, "6 个月"), _describe(s3, "3 个月")) if p]

    sentences = []
    if gold_parts:
        sentences.append(f"比特币与黄金{'、'.join(gold_parts)}")
    if ai_parts:
        prefix = "与 AI 资产" if sentences else "比特币与 AI 资产"
        sentences.append(f"{prefix}{'、'.join(ai_parts)}")

    if not sentences:
        return ""
    return "。".join(sentences) + "。"


def _macro_correlation_data_change(api):
    """PPT 1.3 → SECTION A 宏观数据变化列。
    按 PPT 范例格式：'BTC 与黄金近 3 个月相关性=X, 6 个月相关性=Y。BTC 与 AI 资产近 3 个月相关性=Z, 6 个月相关性=W'。"""
    corr = (api or {}).get("correlations", {}) or {}
    g3 = corr.get("btc_gold_3m")
    g6 = corr.get("btc_gold_6m")
    s3 = corr.get("btc_sox_3m")
    s6 = corr.get("btc_sox_6m")
    if any(v is None for v in (g3, g6, s3, s6)):
        return "—"
    def _fmt(value):
        value = 0.0 if abs(value) < 0.005 else value
        return f"{value:.2f}"
    return (
        f"BTC 与黄金近 3 个月相关性 = {_fmt(g3)}，6 个月相关性 = {_fmt(g6)}。"
        f"BTC 与 AI 资产近 3 个月相关性 = {_fmt(s3)}，6 个月相关性 = {_fmt(s6)}"
    )


def _get_mstr_mnav(data):
    """MSTR mNAV 取数（2026-07-04 用户拍板换主源）：
    主源 = 自算管线 strategy_data.mnav.current（官方稀释股本 + yfinance，股权口径，同图 4）；
    fallback = bitcointreasuries 快照 mnav_data.json（2026-07-03 起该源 SSL 挂，留作兜底，EV 口径偏高 ~0.3x）。"""
    cur = ((data.get("strategy_data") or {}).get("mnav") or {}).get("current")
    if cur is not None:
        return cur
    mnav = data.get("mnav_data", {}) or {}
    for c in mnav.get("snapshot", mnav.get("companies", [])) or []:
        if (c.get("ticker") or "").upper() == "MSTR":
            return c.get("mnav")
    return None


def _dat_one_liner(data, extra):
    """PPT 4.4 / slide 4 → SECTION A 行 2 + SECTION B 因素 2 DAT section 一句话总结 SSOT。
    '回答 2 个关键问题：1）DAT 公司买入卖出节奏是否变化，2）是否有 DAT 公司面临流动性风险'。
    "依据 DAT 公司 section 图片和文字"。"""
    # 矿企抛售结构性观察：2026-07-04 起改为 Phase 3 用户填第 5 项（miner_selloff_note），
    # 值只存 data/user_inputs_cache.json；用户清空/跳过 → 整段不追加。
    miner_note = ((extra.get("user_inputs") or {}).get("miner_selloff_note") or "").strip()
    miner_suffix = f"{miner_note.rstrip('。')}。" if miner_note else ""
    # AI override (extra.dat_one_liner) 已删除 2026-05-21——只保留用户 input 通道
    override = (extra.get("user_inputs") or {}).get("dat_summary")
    if override:
        return f"{override.rstrip('。 ')}。{miner_suffix}"
    dat_data = data.get("dat_data", {}) or {}
    dat_total = dat_data.get("total_cost_summary") or {}
    weighted_cost = dat_total.get("weighted_avg_cost_usd")
    cur_btc = (data.get("api_data", {}).get("btc_price") or {}).get("current") or 0
    # 节奏：从 dat_announcements 近 30d 买入频次判断
    ann = extra.get("dat_announcements") or []
    recent_buys = [a for a in ann if (
        a.get("action") == "buy" if "action" in a else
        "买" in a.get("text", "") or "bought" in a.get("text", "").lower() or "added" in a.get("text", "").lower()
    )]
    if len(recent_buys) >= 5:
        flow_desc = "较高水平（近 30d 多笔大额买入）"
    elif len(recent_buys) >= 2:
        flow_desc = "中等水平"
    else:
        flow_desc = "偏低水平"
    # 流动性风险：MSTR mNAV 接近 1
    mstr_mnav = _get_mstr_mnav(data)
    risk_phrase = ""
    if mstr_mnav is not None:
        if mstr_mnav < 1.0:
            risk_phrase = "。Tier1 DAT 公司 mNAV 已低于净资产，继续发股买币可能产生稀释，融资与流动性风险上升"
        elif mstr_mnav < 1.2:
            risk_phrase = "。Tier1 DAT 公司 mNAV 接近净资产，融资与流动性能力面临考验"
        else:
            risk_phrase = "。Tier1 DAT 公司 mNAV 仍有溢价，未见该指标触发流动性风险信号"
    cost_phrase = ""
    if weighted_cost and cur_btc:
        if weighted_cost > cur_btc:
            cost_phrase = "。追踪公司美元可比部分加权成本高于现价，处于账面浮亏"
        else:
            cost_phrase = "。追踪公司美元可比部分加权成本低于现价，处于账面浮盈"
    return f"DAT 公司近期买入节奏处于{flow_desc}{cost_phrase}{risk_phrase}。{miner_suffix}"


def _dat_data_change(data, extra):
    """PPT 1.7 → SECTION A 行 2 DAT 数据变化列。
    按 PPT 范例格式：'DAT 公司每周买入量在历史中位，环比稳定。DAT 公司存量成本 $74,765，低于现价。DAT 公司股价对 NAV 溢价 1 左右，环比稳定'。"""
    # AI override (extra.dat_data_change) 已删除 2026-05-21——强制走 helper 按 PPT 1.7 范例生成
    dat_data = data.get("dat_data", {}) or {}
    weighted_cost = (dat_data.get("total_cost_summary") or {}).get("weighted_avg_cost_usd")
    cur_btc = (data.get("api_data", {}).get("btc_price") or {}).get("current") or 0
    mstr_mnav = _get_mstr_mnav(data)

    # 与 DAT 周度图同口径：每个 ISO 周取最后一次总持仓快照，周买入量=相邻周末持仓差。
    aggregate = (data.get("dat_history", {}) or {}).get("__aggregate__") or []
    weekly_holdings = {}
    if aggregate:
        from datetime import datetime as _dt
        for row in sorted(aggregate, key=lambda r: str(r.get("date") or "")):
            try:
                d = _dt.strptime(str(row.get("date") or "")[:10], "%Y-%m-%d")
                holdings = float(row.get("total_holdings"))
            except (TypeError, ValueError):
                continue
            iso_year, iso_week, _ = d.isocalendar()
            weekly_holdings[f"{iso_year}-W{iso_week:02d}"] = holdings
    week_ends = [weekly_holdings[k] for k in sorted(weekly_holdings)]
    weekly_buys = [week_ends[i] - week_ends[i - 1] for i in range(1, len(week_ends))]
    if weekly_buys:
        current_week = weekly_buys[-1]
        pct = sum(v <= current_week for v in weekly_buys) / len(weekly_buys) * 100
        if len(weekly_buys) >= 2:
            previous_week = weekly_buys[-2]
            if previous_week == 0:
                if current_week > 0:
                    week_direction = "明显上升（前期为零）"
                elif current_week < 0:
                    week_direction = "明显下降（前期为零）"
                else:
                    week_direction = "稳定"
            else:
                week_delta = (current_week - previous_week) / abs(previous_week)
                if week_delta >= 0.30:
                    week_direction = "明显上升"
                elif week_delta >= 0.10:
                    week_direction = "温和上升"
                elif week_delta <= -0.30:
                    week_direction = "明显下降"
                elif week_delta <= -0.10:
                    week_direction = "温和下降"
                else:
                    week_direction = "稳定"
        else:
            week_direction = "待算"
        weekly_str = f"DAT 公司每周买入量在历史 {pct:.0f}% 分位，环比 {week_direction}"
    else:
        weekly_str = "DAT 公司每周买入量历史分位与环比待算"

    cost_str = f"四家追踪公司美元可比部分存量成本 ${weighted_cost:,.0f}，{'低于' if cur_btc and weighted_cost < cur_btc else '高于'}现价" if weighted_cost else "四家追踪公司美元可比成本未披露"
    mnav_history = (data.get("mnav_history", {}) or {}).get("MSTR") or []
    mnav_values = [float(r["mnav"]) for r in mnav_history if r.get("mnav") is not None]
    if mstr_mnav is not None and len(mnav_values) >= 2 and mnav_values[-2] != 0:
        mnav_delta = (mstr_mnav - mnav_values[-2]) / abs(mnav_values[-2])
        if mnav_delta >= 0.10:
            mnav_direction = "明显上升"
        elif mnav_delta <= -0.10:
            mnav_direction = "明显下降"
        else:
            mnav_direction = "稳定"
        mnav_str = f"DAT 公司股价对 NAV 溢价 {mstr_mnav:.2f}x（Strategy），环比 {mnav_direction}"
    elif mstr_mnav is not None:
        mnav_str = f"DAT 公司股价对 NAV 溢价 {mstr_mnav:.2f}x（Strategy），环比待算"
    else:
        mnav_str = "DAT 公司股价对 NAV 溢价 —，环比待算"
    return f"{weekly_str}。{cost_str}。{mnav_str}"


def _etf_one_liner(data, extra):
    """PPT 7.2 / slide 7 → SECTION A 行 3 + SECTION B 因素 3 ETF section 一句话总结 SSOT。
    '总结 ETF flow 近期有没有明显变化。依据 ETF section 图片、数据和文字'。"""
    # AI override (extra.etf_one_liner) 已删除 2026-05-21——只保留用户 input 通道
    override = (extra.get("user_inputs") or {}).get("etf_summary")
    if override:
        return override
    etf = data.get("etf_flows", {}) or {}
    es = etf.get("summary", {}) or {}
    s7 = es.get("sum_7d_m") or 0
    s30 = es.get("sum_30d_m") or 0
    # 趋势判断
    if abs(s7) > 1000 and abs(s30) > 3000:
        if s7 > 0 and s30 > 0:
            phrase = "ETF flow 近期变化为持续大幅净流入，机构买盘活跃"
        elif s7 < 0 and s30 < 0:
            phrase = "ETF flow 近期变化为明显大幅净流出"
        else:
            phrase = "ETF flow 近期变化为短期与中期方向分化"
    elif s7 > 0 and s30 > 0:
        phrase = "ETF flow 近期变化为持续温和净流入"
    elif s7 < 0 and s30 < 0:
        phrase = "ETF flow 近期变化为持续小幅净流出"
    elif s7 > 0 and s30 < 0:
        phrase = "ETF flow 近期变化为 7 日窗口转为净流入，但 30 日仍累计净流出"
    elif s7 < 0 and s30 > 0:
        phrase = "ETF flow 近期变化为 7 日窗口转为净流出，但 30 日仍累计净流入"
    else:
        phrase = "ETF flow 近期变化为波动，无明显方向"
    return phrase


def _etf_data_change(data, extra):
    """PPT 1.9 → SECTION A 行 3 ETF 数据变化列。
    按 PPT 范例：'ETF 公司每周买入量在历史中位，环比明显降低。7 日累计 $-1,410M·30 日累计 $+2,452M'。
    "环比"含义：当前 ISO 周净流入 vs 上一个 ISO 周净流入。"""
    # AI override (extra.etf_data_change) 已删除 2026-05-21——强制走 helper 按 PPT 1.9 范例 + 周度环比生成
    etf = data.get("etf_flows", {}) or {}
    es = etf.get("summary", {}) or {}
    s7 = es.get("sum_7d_m") or 0
    s30 = es.get("sum_30d_m") or 0
    flows = etf.get("daily_flows", []) or []
    weekly_sums = {}
    if flows:
        from datetime import datetime as _dt
        for row in flows:
            try:
                d = _dt.strptime(str(row.get("date") or "")[:10], "%Y-%m-%d")
                flow = float(row.get("total_flow_m") or row.get("net_flow_m") or 0)
            except (TypeError, ValueError):
                continue
            iso_year, iso_week, _ = d.isocalendar()
            wkey = f"{iso_year}-W{iso_week:02d}"
            weekly_sums[wkey] = weekly_sums.get(wkey, 0) + flow
    week_values = [weekly_sums[k] for k in sorted(weekly_sums)]
    if week_values:
        current_week = week_values[-1]
        pct = sum(v <= current_week for v in week_values) / len(week_values) * 100
        pct_str = f"历史 {pct:.0f}% 分位"
        if len(week_values) >= 2:
            previous_week = week_values[-2]
            if previous_week == 0:
                if current_week > 0:
                    direction = "明显上升（前期为零）"
                elif current_week < 0:
                    direction = "明显下降（前期为零）"
                else:
                    direction = "稳定"
            else:
                delta_pct = (current_week - previous_week) / abs(previous_week) * 100
                if current_week >= 0 > previous_week:
                    direction = "大幅上升（前期流出转流入）"
                elif current_week < 0 <= previous_week:
                    direction = "大幅下降（前期流入转流出）"
                elif delta_pct >= 30:
                    direction = "明显上升"
                elif delta_pct >= 10:
                    direction = "温和上升"
                elif delta_pct <= -30:
                    direction = "明显下降"
                elif delta_pct <= -10:
                    direction = "温和下降"
                else:
                    direction = "稳定"
        else:
            direction = "待算"
    else:
        pct_str = "数据累积中"
        direction = "待算"
    return f"ETF 每周买入量在{pct_str}，环比 {direction}。7 日累计 ${s7:+,.0f}M · 30 日累计 ${s30:+,.0f}M"


def _whale_data_change(brk, whales_pct):
    """PPT 1.11 → SECTION A 行 4 Whales 数据变化列。
    按 PPT 范例：'30d 鲸鱼供应 -80,656 BTC；持有占比 35.39%（30d -0.427%）'。"""
    accum = (brk or {}).get("accumulation", {}) or {}
    w30 = accum.get("whale_30d_change")
    cur = whales_pct.get("current")
    d30 = whales_pct.get("change_30d")
    parts = []
    if w30 is not None:
        parts.append(f"30d 鲸鱼供应 {w30:+,.0f} BTC")
    if cur is not None:
        if d30 is not None:
            parts.append(f"持有占比 {cur:.2f}%（30d {d30:+.3f}%）")
        else:
            parts.append(f"持有占比 {cur:.2f}%")
    return "；".join(parts) if parts else "—"


def _whale_news_summary(extra):
    """PPT 8.3 → 因素 4 Whales section "Whales 新闻汇总"段。
    "就是 websearch 出的新闻卡片上的结果的简要总结"。
    读 SSOT 资金面分析 5 卡的 ① whale_summary 字段，拼成简要总结。"""
    opinions = (extra or {}).get("opinions", {}) or {}
    fund_cards = opinions.get("资金面分析", []) or []
    bullets = []
    for c in fund_cards:
        ws = c.get("whale_summary") or ""
        if not ws:
            continue
        name = c.get("name") or "—"
        # 取每张卡 whale_summary 第 1 句作为简要
        first_sentence = ws.split("。")[0][:180]
        bullets.append(f"{name}：{first_sentence}。")
    return bullets or None


def _policy_one_liner(data, extra):
    """PPT 14.1 / slide 14 → SECTION A 行 7 + SECTION B 因素 8 政府 section 一句话总结 SSOT。
    '验证 1-3 如果有回答，粘贴在这里。后面再跟验证问题 4 的回答的总结，也粘贴在这里。'
    所以 concat 顺序：verify_4 (lead, 立法概率) + verify_1 + verify_2 + verify_3 (跳过 '无' 回答)。"""
    # AI override (extra.policy_one_liner) 已删除 2026-05-21——只保留用户 input 通道
    override = (extra.get("user_inputs") or {}).get("legislation_summary")
    if override:
        return override

    def _short(t, limit=140):
        if not t:
            return None
        t = (t or "").replace("\n", " ").strip()
        if t == "无":
            return None
        return (t[:limit] + "...") if len(t) > limit else t

    def _pick(key, limit):
        """优先用 Phase 2 写的 tldr 真提炼（2026-07-04 用户拍板，替代字符硬截断）；
        tldr 缺失才回退 _short 截断——回退会出 '...'，报告里见省略号 = 该题 tldr 没写。"""
        t = (extra.get(f"{key}_tldr") or "").strip()
        if t and t != "无":
            return t.rstrip("。 ")
        return _short(extra.get(key), limit)

    pieces = []
    # PPT 14.1 lead with 验证 4 (Clarity Act 概率)
    v4 = _pick("verify_4_clarity_passage_probability", 140)
    if v4:
        pieces.append(v4)
    # 验证 1-3 接 concat（跳过 '无'）
    for key in ("verify_1_strategic_reserve", "verify_2_trump_clearance", "verify_3_institutional_entry"):
        v = _pick(key, 120)
        if v:
            pieces.append(v)
    if not pieces:
        return "Clarity Act 立法预期目前是乐观的；BTC 战略储备实施细节预计未来几周公告"
    return "；".join(pieces)


def _filter_recent_announcements(items, days=30):
    """PPT 4.5：DAT 新闻 '仅保留最近一个月的'。
    输入 cache items（list of dict with 'date' field），输出过去 N 天内的项。
    非 dict 或缺 date 字段的项 → 保留（向后兼容）。"""
    if not items:
        return items
    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.now() - _td(days=days)
    filtered = []
    for it in items:
        if not isinstance(it, dict):
            filtered.append(it)
            continue
        d = it.get("date")
        if not d:
            filtered.append(it)
            continue
        try:
            d_obj = _dt.strptime(str(d)[:10], "%Y-%m-%d")
            if d_obj >= cutoff:
                filtered.append(it)
        except Exception:
            filtered.append(it)
    return filtered


def _opinions_one_liner(extra):
    """PPT 16.1 / slide 16 → SECTION A 行 KOL + SECTION C 观点跟踪 section 一句话总结 SSOT。
    '基于 websearch 出的卡片内容，总结老鸟看法、空头看法、对冲基金看法'。"""
    # v3.5 2026-05-21: extra.opinion_one_liner 只能来自 SSOT 自动 set（load_extra line 195-200），
    # AI stdin 直传的同名 key 已在 load 时被 pop。user_inputs.opinion_summary 是用户填充通道。
    override = extra.get("opinion_one_liner") or (extra.get("user_inputs") or {}).get("opinion_summary")
    if override:
        return override
    opinions = (extra or {}).get("opinions", {}) or {}
    bull = opinions.get("币圈老鸟，一般倾向看多", []) or []
    bear = opinions.get("币圈的空头", []) or []
    hedge = opinions.get("对冲基金，一般中性偏多", []) or []
    parts = []
    if bull:
        parts.append(f"看多阵营（{len(bull)} 位）：BTC 目标价 $100K-$300K 区间分歧大，认为美国监管转向带来的机构持续配置会导致本轮熊市比较浅、比较短")
    if bear:
        parts.append(f"看空阵营（{len(bear)} 位）：聚焦 BTC 跑输 AI / 贵金属 + ETF / DAT 驱动的上涨模式可持续性质疑")
    if hedge:
        parts.append(f"对冲基金（{len(hedge)} 位）：2026 仓位普遍降温但维持长期看多")
    return "。".join(parts)


def _whale_one_liner_ai(api, brk, whales_pct):
    """PPT slide 8 因素 4 一句话总结：whales 在 [时间][价格] 大笔[买/卖]。
    基于 BRK 鲸鱼 30d delta + whales% 30d 变化 + 当前 BTC 价格生成。"""
    accum = brk.get("accumulation", {}) or {}
    whale_30d = accum.get("whale_30d_change")
    pct_30d = whales_pct.get("change_30d")
    cur_price = (api.get("btc_price", {}) or {}).get("current") or 0
    if whale_30d is None and pct_30d is None:
        return None
    direction = "卖出" if (whale_30d and whale_30d < 0) or (pct_30d and pct_30d < 0) else "买入"
    parts = [f"近 30 天 whales（≥1k BTC 地址）在 BTC ${cur_price:,.0f} 价位附近出现明显大笔{direction}"]
    if whale_30d is not None:
        parts.append(f"30d 鲸鱼供应 {whale_30d:+,.0f} BTC")
    if pct_30d is not None:
        parts.append(f"持有占比 30d {pct_30d:+.3f}%")
    return "；".join(parts)


def _build_cm_exchange_reserve_chart(cm_data, ahr_data=None):
    """Coin Metrics SplyExNtv 17 年 daily → 周度采样 (~900 点) 喂给 Chart.js。
    同时输出匹配日期的 BTC 价格序列（来自 ahr999.json）做双轴。"""
    if not cm_data or "series" not in cm_data:
        return {"chart_cm_xres_labels": "[]", "chart_cm_xres_values": "[]",
                "chart_cm_xres_btc_price": "[]", "cm_xres_latest": None}
    series = cm_data["series"].get("exchange_reserve_btc", [])
    # 周度采样：每 7 天 1 个非 null 点
    sampled = []
    for i in range(0, len(series), 7):
        for j in range(min(i + 6, len(series) - 1), i - 1, -1):
            if series[j]["value"] is not None:
                sampled.append(series[j])
                break
    labels = [r["date"] for r in sampled]
    values = [round(r["value"], 0) if r["value"] else None for r in sampled]
    # 匹配 BTC 价格 (右轴)：用 ahr999 的 dates + btc_prices 建索引，缺失日向前回退 ≤7 天
    btc_price_aligned = []
    if ahr_data:
        ahr_dates = ahr_data.get("dates") or []
        ahr_prices = ahr_data.get("btc_prices") or []
        price_by_date = {ahr_dates[i]: ahr_prices[i]
                         for i in range(min(len(ahr_dates), len(ahr_prices)))
                         if ahr_prices[i]}
        from datetime import datetime as _dt, timedelta as _td
        for lbl in labels:
            if lbl in price_by_date:
                btc_price_aligned.append(round(float(price_by_date[lbl]), 0))
                continue
            # 兜底：往前回退最多 7 天
            try:
                d_obj = _dt.strptime(lbl, "%Y-%m-%d")
                hit = None
                for back in range(1, 8):
                    prev = (d_obj - _td(days=back)).strftime("%Y-%m-%d")
                    if prev in price_by_date:
                        hit = round(float(price_by_date[prev]), 0)
                        break
                btc_price_aligned.append(hit)
            except Exception:
                btc_price_aligned.append(None)
    latest = dict(cm_data.get("latest", {}) or {})
    # PPT slide 9 caption 需要 1 年变化；fetcher 的 latest 目前只带 30d，
    # 因此从同一条 Coin Metrics daily 序列现场计算，不混用其他数据源。
    if latest.get("exchange_reserve_btc") is not None and latest.get("exchange_reserve_btc_1y_delta") is None:
        from datetime import datetime as _dt, timedelta as _td
        try:
            latest_date = _dt.strptime(str(latest.get("date"))[:10], "%Y-%m-%d")
            target_date = latest_date - _td(days=365)
            prior = None
            for row in series:
                if row.get("value") is None:
                    continue
                row_date = _dt.strptime(str(row.get("date"))[:10], "%Y-%m-%d")
                if row_date <= target_date:
                    prior = float(row["value"])
                else:
                    break
            if prior is not None:
                latest["exchange_reserve_btc_1y_delta"] = float(latest["exchange_reserve_btc"]) - prior
        except (TypeError, ValueError):
            pass
    return {
        "chart_cm_xres_labels": json.dumps(labels),
        "chart_cm_xres_values": json.dumps(values),
        "chart_cm_xres_btc_price": json.dumps(btc_price_aligned),
        "cm_xres_latest": latest,
        "cm_xres_history_start": cm_data.get("history_start"),
        "cm_xres_rows": cm_data.get("rows"),
    }


def _exchange_reserve_signal(brk):
    """PPT slide 9 因素 4 图 2：Exchange Reserve 近 1 个月下降 → 利好，反之 → 利空。
    用 BRK 鲸鱼供应 30d 变化反向代理（鲸鱼地址余额增加 ≈ 币离开交易所）。"""
    accum = brk.get("accumulation", {}) or {}
    whale_30d = accum.get("whale_30d_change")
    if whale_30d is None:
        return None
    if whale_30d > 0:
        return f"鲸鱼地址 30d 增加 {whale_30d:+,.0f} BTC（约等于币离开交易所）→ **利好** 价格"
    return f"鲸鱼地址 30d 减少 {whale_30d:+,.0f} BTC（约等于币进入交易所）→ **利空** 价格"


def _avg_funding(hist_8h, days):
    if not hist_8h:
        return None
    n = days * 3  # 3 windows per day
    window = hist_8h[-n:] if len(hist_8h) >= n else hist_8h
    if not window:
        return None
    avg = sum(item["rate_8h"] for item in window) / len(window)
    return round(avg, 4)


def _funding_streak(f):
    streaks = f.get("negative_streak", [])
    if streaks:
        last = streaks[-1]
        return f"最近负值连续: {last['days']}天 ({last['start']})"
    return "无连续负值"


# ── Chart data serialization ─────────────────────────────────

def prepare_chart_data(data):
    api = data.get("api_data", {})
    etf = data.get("etf_flows", {})
    brk = data.get("brk_data", {})
    deriv = data.get("derivatives_data", {})
    mnav_hist = data.get("mnav_history", {})
    dat_hist = data.get("dat_history", {})

    btc_hist = api.get("btc_price", {}).get("history", [])
    gold_hist = api.get("gold_price", {}).get("history", [])
    sox_hist = api.get("sox_price", {}).get("history", [])

    # 2026-05-20 bug fix：用 YYYY-MM-DD 作 join key（之前用 "%m-%d" 跨年时 05-16 等日期冲突
    # 导致 chart 末尾几点错位映射回 2025 旧数据 → SOX/Gold 折线垂直跳水）。
    # x 轴 tick 显示仍用 "mm-dd" 简短格式，但 join 用 full date。
    btc_full_dates = [datetime.fromtimestamp(p["ts"]/1000).strftime("%Y-%m-%d") for p in btc_hist]
    btc_labels = [d[5:] for d in btc_full_dates]  # "05-20" for x-axis display
    btc_values = [p["price"] for p in btc_hist]

    # Align gold/sox to btc dates (用 full YYYY-MM-DD 避免跨年同 mm-dd 冲突)
    gold_by_date = {}
    for p in gold_hist:
        d = datetime.fromtimestamp(p["ts"]/1000).strftime("%Y-%m-%d")
        gold_by_date[d] = p["price"]
    gold_values = [gold_by_date.get(d) for d in btc_full_dates]

    sox_by_date = {}
    for p in sox_hist:
        d = datetime.fromtimestamp(p["ts"]/1000).strftime("%Y-%m-%d")
        sox_by_date[d] = p["price"]
    sox_values = [sox_by_date.get(d) for d in btc_full_dates]

    # ETF
    flows = etf.get("daily_flows", [])
    etf_labels = [f["date"] for f in flows[-30:]]
    etf_values = [f["total_flow_m"] for f in flows[-30:]]

    # Funding rate — 2 年时序（740 天，Hyperliquid history_8h 全量保留）+ BTC 价格右轴
    fr_hist = api.get("funding", {}).get("history_8h", [])
    fr_labels = [f["date"] for f in fr_hist]
    fr_values = [f["rate_8h"] for f in fr_hist]
    # 匹配 BTC 价格右轴：funding label 是 "YYYY-MM-DD HH:MM"，BTC 价是 daily，取日期前缀匹配
    ahr_dates_funding = (data.get("ahr999", {}) or {}).get("dates") or []
    ahr_prices_funding = (data.get("ahr999", {}) or {}).get("btc_prices") or []
    price_by_date_funding = {
        ahr_dates_funding[i]: ahr_prices_funding[i]
        for i in range(min(len(ahr_dates_funding), len(ahr_prices_funding)))
        if ahr_prices_funding[i]
    }
    fr_btc_price = []
    from datetime import datetime as _dt_fr, timedelta as _td_fr
    for lbl in fr_labels:
        date_only = lbl[:10]  # "YYYY-MM-DD"
        price = price_by_date_funding.get(date_only)
        if not price:
            try:
                d_obj = _dt_fr.strptime(date_only, "%Y-%m-%d")
                for back in range(1, 8):
                    prev = (d_obj - _td_fr(days=back)).strftime("%Y-%m-%d")
                    if prev in price_by_date_funding:
                        price = price_by_date_funding[prev]
                        break
            except Exception:
                pass
        fr_btc_price.append(round(float(price), 0) if price else None)

    # Stablecoin
    stable_hist = api.get("stablecoins", {}).get("history", [])
    stable_labels = [h["date"] for h in stable_hist]
    stable_values = [round(h["total"] / 1e9, 2) for h in stable_hist]

    # BRK whale supply chart (>=1k BTC addresses)
    brk_dates = brk.get("dates", []) or []
    brk_whale = (brk.get("series") or {}).get("whale_supply", []) or []
    brk_lth = (brk.get("series") or {}).get("lth_supply", []) or []
    brk_mvrv = (brk.get("series") or {}).get("mvrv", []) or []

    # OKX OI history (100d)
    okx = deriv.get("okx_oi_history", {}) or {}
    oi_labels = okx.get("dates", []) or []
    oi_values = [round(v / 1e9, 3) for v in (okx.get("oi_usd") or [])]

    # mNAV history per ticker (only featured DATs)
    mnav_chart = {}
    featured_tickers = ["MSTR", "MPJPY", "MARA", "XXI", "BLSH"]
    for t in featured_tickers:
        rows = mnav_hist.get(t, []) or []
        if rows:
            mnav_chart[t] = {
                "labels": [r["date"] for r in rows],
                "values": [r["mnav"] for r in rows],
            }

    # DAT aggregate history (only if cron has run multiple days)
    dat_agg = dat_hist.get("__aggregate__", []) or []
    dat_agg_chart = {
        "labels": [r["date"] for r in dat_agg],
        "total_holdings": [r["total_holdings"] for r in dat_agg],
    }

    # DAT per-company history (the four report-tracked companies)
    dat_per_company = {}
    featured_dats = ["Strategy", "Twenty One Capital", "Metaplanet", "MARA Holdings"]
    for name in featured_dats:
        rows = dat_hist.get(name, []) or []
        if rows:
            dat_per_company[name] = {
                "labels": [r["date"] for r in rows],
                "holdings": [r["holdings"] for r in rows],
            }

    # Tether history time-series
    tether_db_path = Path(__file__).parent.parent / "data" / "tether_history.sqlite"
    tether_hist_chart = {"labels": [], "btc_holdings": []}
    if tether_db_path.exists():
        import sqlite3 as _sql
        try:
            conn = _sql.connect(tether_db_path)
            cur = conn.execute("SELECT date, btc_holdings FROM tether_snapshots ORDER BY date")
            rows = cur.fetchall()
            conn.close()
            tether_hist_chart["labels"] = [r[0] for r in rows]
            tether_hist_chart["btc_holdings"] = [r[1] for r in rows]
        except Exception:
            pass

    # Market aggregate (CoinGecko derivatives) accumulating history → items 8/9
    deriv_market_hist = (data.get("derivatives_data", {}) or {}).get("market_aggregate_history", []) or []
    # 2026-07-03 TODO #10 解决：优先 Coinalyze 头部 6 所 4 年聚合；SQLite 累积（CoinGecko 快照）降级 fallback
    _cz = (data.get("derivatives_data", {}) or {}).get("coinalyze_oi_agg", {}) or {}
    if _cz.get("dates"):
        deriv_hist_chart = {
            "labels": _cz["dates"],
            "oi_usd_b": [round(v / 1e9, 3) for v in _cz["oi_usd"]],
            "funding_pct": [],
            "source_note": "coinalyze",
        }
    else:
        deriv_hist_chart = {
            "labels": [r["date"] for r in deriv_market_hist],
            "oi_usd_b": [round((r["total_oi_usd"] or 0) / 1e9, 3) for r in deriv_market_hist],
            "funding_pct": [r["funding_pct"] for r in deriv_market_hist],
            "source_note": "sqlite",
        }

    return {
        "chart_btc_labels": json.dumps(btc_labels),
        "chart_btc_values": json.dumps(btc_values),
        "chart_gold_values": json.dumps(gold_values),
        "chart_sox_values": json.dumps(sox_values),
        "chart_etf_labels": json.dumps(etf_labels),
        "chart_etf_values": json.dumps(etf_values),
        "chart_fr_labels": json.dumps(fr_labels),
        "chart_fr_values": json.dumps(fr_values),
        "chart_fr_btc_price": json.dumps(fr_btc_price),
        "chart_stable_labels": json.dumps(stable_labels),
        "chart_stable_values": json.dumps(stable_values),
        # New charts
        "chart_brk_dates": json.dumps(brk_dates),
        "chart_brk_whale": json.dumps(brk_whale),
        "chart_brk_lth": json.dumps(brk_lth),
        "chart_brk_mvrv": json.dumps(brk_mvrv),
        "chart_oi_labels": json.dumps(oi_labels),
        "chart_oi_values": json.dumps(oi_values),
        "chart_mnav": json.dumps(mnav_chart),
        "mnav_history_days": len({r["date"] for ticker_rows in mnav_hist.values() if isinstance(ticker_rows, list) for r in ticker_rows if isinstance(r, dict) and r.get("date")}),
        "chart_dat_labels": json.dumps(dat_agg_chart["labels"]),
        "chart_dat_holdings": json.dumps(dat_agg_chart["total_holdings"]),
        "chart_dat_per_company": json.dumps(dat_per_company),
        "chart_tether_labels": json.dumps(tether_hist_chart["labels"]),
        "chart_tether_holdings": json.dumps(tether_hist_chart["btc_holdings"]),
        "chart_market_labels": json.dumps(deriv_hist_chart["labels"]),
        "chart_market_oi": json.dumps(deriv_hist_chart["oi_usd_b"]),
        "market_oi_source": deriv_hist_chart.get("source_note"),
        "chart_market_funding": json.dumps(deriv_hist_chart["funding_pct"]),
    }


# ── Key companies list ───────────────────────────────────────

def prepare_key_companies(tres):
    kc = tres.get("key_companies", {})
    order = [
        "Strategy", "Twenty One Capital", "Metaplanet Inc",
        "MARA Holdings, Inc", "Tether Holdings Limited",
        "Block", "Tesla", "Coinbase",
    ]
    result = []
    for name in order:
        co = kc.get(name, {})
        if co.get("btc_holdings"):
            result.append({
                "name": co.get("name", name),
                "btc_holdings": co.get("btc_holdings"),
                "market_cap_usd": co.get("market_cap_usd"),
                "avg_cost_usd": co.get("avg_cost_usd"),
                "mnav": co.get("mnav"),
            })
    return result


# ── Opinions ─────────────────────────────────────────────────

# 观点卡片名单（含用户批准的后续增补）；数量由名单计算。
FULL_KOL_ROSTER = {
    "币圈老鸟，一般倾向看多": [
        {"name": "CZ", "org": "Binance 创始人", "twitter": "cz_binance"},
        {"name": "Star Xu", "org": "OKX 创始人", "twitter": "star_okx"},
        {"name": "Vitalik Buterin", "org": "Ethereum 联合创始人", "twitter": "VitalikButerin"},
        {"name": "Brad Garlinghouse", "org": "Ripple CEO", "twitter": "bgarlinghouse"},
        {"name": "Garrett Jin", "org": "Bitget 前 CMO / 1011 Insider Whale", "twitter": "GarrettBullish"},
        {"name": "Tom Lee", "org": "BMNR / Fundstrat", "twitter": "fundstrat"},
        {"name": "Joseph Chalom", "org": "SharpLink Gaming（前 BlackRock）", "twitter": "joechalom"},
        {"name": "Peter Thiel", "org": "Founders Fund", "twitter": None},
        {"name": "Trendresearch", "org": "中文加密研究 (Medium)", "twitter": None, "rss": "https://trendresearch.medium.com/feed"},
        {"name": "Matt Hougan", "org": "Bitwise CIO", "twitter": "Matt_Hougan"},
        {"name": "VanEck", "org": "数字资产研究 · Matthew Sigel；有 BTC 敞口背景", "twitter": "matthew_sigel", "website": "https://www.vaneck.com/us/en/insights/thought-leaders/matthew-sigel"},
        {"name": "David Sacks", "org": "白宫 AI & Crypto Czar / Craft Ventures", "twitter": "DavidSacks"},
        {"name": "Patrick Witt", "org": "白宫数字资产顾问委员会执行主任", "twitter": None},
    ],
    "币圈的空头": [
        {"name": "Brett Knoblauch", "org": "Cantor Fitzgerald", "twitter": None},
        {"name": "Aaron Day", "org": "Daylight Freedom 主席", "twitter": "AaronRDay"},
        {"name": "Jemima Kelly", "org": "FT 专栏", "twitter": "jemimajoanna"},
        {"name": "Peter Schiff", "org": "SchiffGold / Euro Pacific", "twitter": "PeterSchiff"},
        {"name": "Nassim Taleb", "org": "《黑天鹅》作者", "twitter": "nntaleb"},
    ],
    "对冲基金，一般中性偏多": [
        {"name": "BH Digital (Brevan Howard)", "org": "Brevan Howard 加密部门", "twitter": "BHDigitalAssets"},
        {"name": "Galaxy Digital", "org": "公司号", "twitter": "galaxyhq"},
        {"name": "Mike Novogratz", "org": "Galaxy CEO 个人号", "twitter": "novogratz"},
        {"name": "DRW Trading", "org": "Don Wilson 公司号", "twitter": "DRWTrading"},
        {"name": "Jason Huang", "org": "NextGen CIO", "twitter": None},
    ],
}


def prepare_opinions(extra):
    """按完整名单合并观点；待采集与已查无更新分别展示。"""
    raw = extra.get("opinions", {})
    cat_config = [
        ("币圈老鸟，一般倾向看多", "bullish", "#22c55e"),
        ("币圈的空头", "bearish", "#ef4444"),
        ("对冲基金，一般中性偏多", "hedge", "#a855f7"),
    ]
    categories = []
    has_any = False
    for cat_name, badge_class, color in cat_config:
        roster = FULL_KOL_ROSTER.get(cat_name, [])
        extra_people = raw.get(cat_name, []) or []
        # Index extra by name for merge
        extra_by_name = {p.get("name", ""): p for p in extra_people}
        # Fuzzy 前缀匹配：roster name 如果在 extra name 中作前缀也算匹配（防止 "X (Y)" vs "X" 不严格相等）
        def _find_extra(roster_name):
            if roster_name in extra_by_name:
                return extra_by_name[roster_name]
            for ek, ev in extra_by_name.items():
                if ek.startswith(roster_name + " ") or roster_name.startswith(ek + " ") or ek.split(" (")[0] == roster_name:
                    return ev
            return None
        merged = []
        for person in roster:
            name = person["name"]
            ep = _find_extra(name)
            handle = person.get("twitter")
            tw_url = f"https://x.com/{handle}" if handle else None
            home_url = person.get("website") or tw_url or person.get("rss")
            if ep and ep.get("collection_status") == "pending":
                merged.append({
                    "name": name, "org": person["org"], "date": "—",
                    "source_url": home_url, "summary": "待首次采集",
                    "has_update": False, "status_label": "待首次采集",
                })
                continue
            if ep:
                # 2 周窗过滤（主报告口径）：发言日期超 14d → 视作 has_update=False
                from datetime import datetime as _dt, timedelta as _td
                _today = _dt.now()
                _within = False
                _d_str = ep.get("date", "")
                if _d_str and _d_str != "—":
                    try:
                        _d = _dt.fromisoformat(_d_str)
                        _within = (_today - _d) <= _td(days=14)
                    except Exception:
                        _within = False
                if not _within or ep.get("has_update") is False:
                    # 在 SSOT 里但超 2 周 → 渲染为"本周无更新"占位（保留 source URL 让用户能点过去看）
                    merged.append({
                        "name": name,
                        "org": person["org"],
                        "date": "—",
                        "source": "—",
                        "source_url": person.get("website") or tw_url or ep.get("source_url"),
                        "summary": "本周无更新",
                        "has_update": False,
                    })
                    continue
                # 在窗内：merge handle 真 link
                merged.append({
                    "name": name,
                    "org": ep.get("org") or person["org"],
                    "date": ep.get("date") or "—",
                    "source": ep.get("source") or tw_url or "WebSearch",
                    "source_url": ep.get("source_url") or tw_url,
                    "sources": ep.get("sources") or [],
                    "summary": ep.get("summary") or ep.get("opinion") or "本周无更新",
                    "has_update": True,
                })
                has_any = True
            else:
                # 没观点：占位"本周无更新"
                merged.append({
                    "name": name,
                    "org": person["org"],
                    "date": "—",
                    "source": home_url or "—",
                    "source_url": home_url,
                    "summary": "本周无更新",
                    "has_update": False,
                })
        categories.append({
            "name": cat_name, "badge_class": badge_class,
            "color": color,
            "count": sum(1 for m in merged if m["has_update"]),
            "total": len(merged),
            "people": merged,
        })
    return True, categories


def prepare_analyst_funding_cards(extra):
    """因素 4 末尾的资金面分析卡片（策略组 6 人）：14d 过滤——
    与 KOL 网格同口径：fresh ≤ 14d 用原卡，超 14d 渲染"本周无更新"占位卡（保留 name/org/source_url）。
    无 date / date 格式坏 → 视作 stale。"""
    raw = extra.get("opinions", {})
    candidates = raw.get("资金面分析", []) or []
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.now()
    cutoff = today - _td(days=14)
    out = []
    for p in candidates:
        d_str = (p.get("date") or "").strip()
        is_fresh = False
        if d_str and d_str != "—":
            try:
                d = _dt.fromisoformat(d_str)
                is_fresh = d >= cutoff
            except Exception:
                is_fresh = False
        if is_fresh:
            out.append(p)
        else:
            out.append({
                "name": p.get("name"),
                "org": p.get("org"),
                "handle": p.get("handle"),
                "date": "—",
                "source_url": p.get("source_url"),
                "whale_summary": "本周无更新",
                "market_summary": "本周无更新",
                "stale": True,
            })
    return out


def build_summary_rows(data, extra):
    """SECTION A 本期总结：严格对齐 PPT slide 1 的 8 行 × 3 列结构。

    PPT slide 1 实际结构（重读后核对）：
      行 1 宏观环境       | 一句话:用户填(金融条件+经济位置)              | 后续关注点:AI填(BTC vs 黄金/AI 相关性) | 数据变化:空
      行 2 四年周期       | 一句话:用户填(见底预测日见 CYCLE_BOTTOM_DATE 常量)  | 空                                     | 空
      行 3 DAT 公司动向   | 一句话:AI填(回答买卖节奏+流动性风险)            | 后续关注点:用户填(DAT 彻底出清利好)    | 数据变化:AI填(每周买入分位+存量成本+mNAV溢价)
      行 4 ETF 动向       | 一句话:AI填(基于 ETF section)                  | 空                                     | 数据变化:AI填(ETF 每周买入分位)
      行 5 Whales 动向    | 一句话:AI填(照抄因素 4 一句话)                 | 空                                     | 空
      行 6 情绪指标       | 一句话:AI填(照抄因素 6 一句话)                 | 空                                     | 空
      行 7 美国政府/主流  | 一句话:AI填(照抄因素 8 一句话)                 | 后续关注点:用户填(Trump 清算利好)      | 空
      行 8 KOL 观点变化   | 一句话:AI填(照抄观点跟踪一句话)                | 空                                     | 空
    """
    api = data.get("api_data", {})
    etf = data.get("etf_flows", {})
    brk = data.get("brk_data", {})
    user = extra.get("user_inputs", {})

    corr = api.get("correlations", {})
    fg = data.get("fear_greed", {}) or {}
    ahr = data.get("ahr999", {}) or {}
    etf_sum = etf.get("summary", {})
    accum = brk.get("accumulation", {}) or {}
    whales_pct = (brk.get("whales_pct") or {})
    opinions = extra.get("opinions", {})
    bull_n = len(opinions.get("币圈老鸟，一般倾向看多") or opinions.get("看多 KOL", []))
    bear_n = len(opinions.get("币圈的空头") or opinions.get("看空 KOL", []))
    hedge_n = len(opinions.get("对冲基金，一般中性偏多") or opinions.get("对冲基金", []))

    cycle_bottom = cycle_bottom_from_text(user.get("cycle_summary"))  # 用户填文优先，常量兜底
    cycle_days = max(0, (datetime.strptime(cycle_bottom, "%Y-%m-%d") - datetime.now()).days)
    corr_gold_3m = corr.get("btc_gold_3m") or 0
    corr_gold_6m = corr.get("btc_gold_6m") or 0
    corr_sox_3m = corr.get("btc_sox_3m") or 0
    corr_sox_6m = corr.get("btc_sox_6m") or 0


    rows = []

    # 行 1 宏观环境 — PPT 1.1+1.2+1.3:
    #   一句话总结当前状态 = 用户填(part1) + AI 相关性 helper(part2)
    #   后续关注点 = 空
    #   数据变化 = AI 按 PPT 范例格式（相关系数）
    macro_pieces = []
    if user.get("macro_summary"):
        macro_pieces.append(user["macro_summary"])
    # PPT 1.2 AI 相关性总结（独立 helper，非照抄）
    macro_pieces.append(_macro_correlation_one_liner_ai(api))
    macro_one_default = "。".join(p for p in macro_pieces if p)
    rows.append({
        "label": "宏观环境",
        "user_input_required": True,  # PPT 1.1 明确"只能用户来更新"
        # 仅 part1（user_inputs.macro_summary）走 user-fill cache + badge；part2 是 AI 自动
        "user_input_cells": {"one_liner": "macro_summary"},
        "one_liner": macro_one_default,
        "watch_next": "",
        # PPT 1.3 数据变化按范例格式
        "data_change": _macro_correlation_data_change(api),
    })

    # 行 2 四年周期：user_input_required（PPT 标"用户更新"）
    rows.append({
        "label": "四年周期",
        "user_input_required": True,
        "user_input_cells": {"one_liner": "cycle_summary"},
        "one_liner": user.get("cycle_summary") or f"四年周期指向 {cycle_bottom} 见底（距底 {cycle_days} 天），9 月观察是否提前见底",
        "watch_next": "",
        "data_change": "",
    })

    # 行 3 DAT 公司动向 — PPT 1.5+1.6+1.7
    rows.append({
        "label": "DAT 公司动向",
        "user_input_cells": {"watch_next": "dat_outflow_bullish"},
        # PPT 1.5 SSOT：照抄因素 2 一句话总结
        "one_liner": _dat_one_liner(data, extra),
        "watch_next": user.get("dat_outflow_bullish") or "DAT 公司彻底出清对币价是大利好",
        # PPT 1.7 按范例：每周买入分位 + 存量成本 + mNAV
        "data_change": _dat_data_change(data, extra),
    })

    # 行 4 ETF 动向 — PPT 1.8+1.9
    rows.append({
        "label": "ETF 动向",
        # PPT 1.8 SSOT：照抄因素 3 一句话总结
        "one_liner": _etf_one_liner(data, extra),
        "watch_next": "",
        # PPT 1.9 按范例：每周买入分位 + 7d/30d 累计
        "data_change": _etf_data_change(data, extra),
    })

    # 行 5 Whales 动向 — PPT 1.10+1.11
    # PPT 1.10 SSOT：照抄因素 4 一句话总结（_whale_one_liner_ai）
    # 🟢 extra.whale_one_liner 是**唯一合法的 AI 手写注入点**（SOP phases/5_render.md §5.0 line 713 明文）：
    # helper _whale_one_liner_ai() 只用 BRK 30d delta，PPT 要求 AI 综合 3 源（BRK + whale_buysell_news + 策略组卡）手写。
    # 其他任何 extra.X_one_liner / X_data_change 字段一律不许传（2026-05-21 已堵死）。
    whale_qual = (
        extra.get("whale_one_liner")
        or user.get("whale_summary")
        or _whale_one_liner_ai(api, brk, whales_pct)
    )
    rows.append({
        "label": "Whales 动向",
        "one_liner": whale_qual or "—",
        "watch_next": "",
        # PPT 1.11 按范例：30d 鲸鱼供应 + 持有占比 + 30d 变化
        "data_change": _whale_data_change(brk, whales_pct),
    })

    # 行 6 情绪指标：one_liner = 定性结论（与因素 6 section 共用 helper 保证两处一致），data_change = 数字
    fg_cur = fg.get("current")
    ahr_cur = ahr.get("current")
    funding_obj = api.get("funding", {})
    cvdd_state = _cvdd_state_from_data(data)
    mvrv_z_cur = ((data.get("bgeometrics_data") or {}).get("mvrv_zscore") or {}).get("current")
    sentiment_qual = _sentiment_qualitative(fg_cur, ahr_cur, funding_obj, cvdd_state, mvrv_z_cur)
    sentiment_num = _sentiment_numeric(fg_cur, ahr_cur, funding_obj, cvdd_state, mvrv_z_cur)
    rows.append({
        "label": "情绪指标",
        # AI override (extra.sentiment_one_liner) 已删除 2026-05-21——强制走 _sentiment_qualitative helper
        "one_liner": sentiment_qual,
        "watch_next": "",
        "data_change": sentiment_num,
    })

    # 行 7 美国政府和主流机构态度 — PPT 1.14+1.15
    rows.append({
        "label": "美国政府和主流机构态度",
        "user_input_cells": {"watch_next": "trump_clearance_bullish"},
        # PPT 1.14 SSOT：照抄因素 8 一句话总结
        "one_liner": _policy_one_liner(data, extra),
        "watch_next": user.get("trump_clearance_bullish") or "Trump 等内幕交易者彻底出清、中选后继续推进对比特币友好政策对币价是大利好",
        "data_change": "",
    })

    # 行 8 KOL 观点变化 — PPT 1.16: 照抄观点跟踪 section 一句话总结
    rows.append({
        "label": "币圈 KOL 观点变化",
        "one_liner": _opinions_one_liner(extra),
        "watch_next": "",
        "data_change": "",
    })

    # 给每行打 sentiment tag（基于 one_liner 关键词），template 用来染行底色
    for r in rows:
        r["sentiment"] = _row_sentiment(r.get("one_liner", ""))

    return rows


# ── Build full context ───────────────────────────────────────

def _clarity_milestones_from_policy(policy_data):
    """Convert Congress.gov action history into a milestone list for timeline rendering."""
    clarity = policy_data.get("factor_11_clarity_act", {})
    timeline = clarity.get("timeline", [])
    if not timeline:
        return None
    # Group key events; the API returns newest-first, so reverse for chronological
    chronological = list(reversed(timeline))
    keystones = []
    # Identify high-signal actions
    for a in chronological:
        txt = (a.get("text") or "").lower()
        date = a.get("date")
        chamber = a.get("chamber") or ""
        if "passed" in txt and "house" in chamber.lower():
            keystones.append({"label": "House 通过", "date": date, "state": "done", "detail": a.get("text", "")[:140]})
        elif "received in the senate" in txt:
            keystones.append({"label": "Senate 收到 + 转 Banking 委员会", "date": date, "state": "done", "detail": a.get("text", "")[:140]})
        elif "reported" in txt and "senate" in chamber.lower():
            keystones.append({"label": "Senate Banking 报告", "date": date, "state": "done", "detail": a.get("text", "")[:140]})
        elif "agreed to in senate" in txt:
            keystones.append({"label": "Senate 通过", "date": date, "state": "done", "detail": a.get("text", "")[:140]})
        elif "signed by president" in txt:
            keystones.append({"label": "总统签署", "date": date, "state": "done", "detail": a.get("text", "")[:140]})

    # Always append pending future milestones if not present
    have_labels = {k["label"] for k in keystones}
    pending_template = [
        ("Senate Banking 投票", "active"),
        ("Senate 全院投票", "pending"),
        ("总统签署", "pending"),
    ]
    for label, state in pending_template:
        if label not in have_labels:
            keystones.append({"label": label, "date": "TBD", "state": state, "detail": None})
    return keystones


def _policy_news_from_data(policy_data, extra_news):
    """Combine auto-fetched policy news + manually-curated `extra.news`."""
    news = []
    # Federal Register (factor 9)
    for h in policy_data.get("factor_9_strategic_reserve", [])[:5]:
        if "error" in h:
            continue
        news.append({
            "factor": 9,
            "title": h.get("title"),
            "date": h.get("date"),
            "url": h.get("url"),
            "summary": (h.get("abstract") or "")[:300] or "[Federal Register 自动抓取]",
            "source_auto": True,
        })
    # SEC EDGAR (factor 10)
    for h in policy_data.get("factor_10_institution", [])[:5]:
        if "error" in h:
            continue
        news.append({
            "factor": 10,
            "title": h.get("title"),
            "date": h.get("date"),
            "url": h.get("url"),
            "summary": f"SEC {h.get('form')} filing by {h.get('company')}",
            "source_auto": True,
        })
    # AI-curated overrides come first if present
    combined = (extra_news or []) + news
    # 30d 过滤：与 DAT 4.5 一致，避免老 milestone 长期占位（用户已知信息）
    return _filter_recent_announcements(combined, days=30)


def build_context(data, extra):
    api = data.get("api_data", {})
    etf = data.get("etf_flows", {})
    tres = data.get("treasuries", {})
    brk = data.get("brk_data", {})
    deriv = data.get("derivatives_data", {})
    mnav = data.get("mnav_data", {})
    mnav_hist = data.get("mnav_history", {})
    dat = data.get("dat_data", {})
    dat_hist = data.get("dat_history", {})
    tether = data.get("tether_data", {})
    coinshares = data.get("coinshares_data", {})
    policy = data.get("policy_data", {})
    tweets = data.get("whale_tweets", {})
    user = extra.get("user_inputs", {})

    btc = api.get("btc_price", {})
    funding = api.get("funding", {})
    stable = api.get("stablecoins", {})
    corr = api.get("correlations", {})
    etf_summary = etf.get("summary", {})

    fp = funding.get("percentile_730d")  # 2026-07-04 与 SECTION A 行同步换 730d（90d 基期失真）
    fg = data.get("fear_greed", {}) or {}
    ahr = data.get("ahr999", {}) or {}

    has_opinions, opinion_categories = prepare_opinions(extra)
    analyst_funding_cards = prepare_analyst_funding_cards(extra)

    # Strategy 官方周度净买入；mNAV 改用跨公司统一 EV 口径。
    _strat = data.get("strategy_data", {}) or {}
    _strat_weekly = _strat.get("weekly_net_buys") or {}

    _mnav_names = {"MSTR": "Strategy", "XXI": "Twenty One", "MPJPY": "Metaplanet"}
    _mnav_dates = sorted({
        item.get("date")
        for ticker in _mnav_names
        for item in (mnav_hist.get(ticker) or [])
        if item.get("date")
    })
    _mnav_series = {}
    _mnav_current = {}
    for _ticker, _name in _mnav_names.items():
        _by_date = {
            item.get("date"): item.get("mnav")
            for item in (mnav_hist.get(_ticker) or [])
            if item.get("date") and item.get("mnav") is not None
        }
        _mnav_series[_ticker] = [_by_date.get(d) for d in _mnav_dates]
        if _by_date:
            _mnav_current[_ticker] = _by_date[sorted(_by_date)[-1]]
    _mnav_methodology = mnav.get("methodology") or {}

    _tether_quarterly_history = tether.get("quarterly_history") or []
    _tether_quarterly_history = [
        row for row in _tether_quarterly_history
        if row.get("quarter") and row.get("btc_count") is not None
    ]

    # whales% 周度数据（用于 chartWhalesPct，PPT 因素 4 图 1）
    # 2026-07-01 升级：优先用 brk.whales_pct_long（5.5 年 / 2000d，替换 CoinShares Exhibit 35 截图），
    # BTC 价格按【日期精确配对】ahr999.btc_prices（2016-12 起 10 年，全覆盖）。旧 365d + offset 对齐法弃用。
    whales_pct = (brk.get("whales_pct") or {})
    _wl = brk.get("whales_pct_long") or {}
    _wl_dates = _wl.get("dates") or []
    _wl_values = _wl.get("values") or []
    if not _wl_dates:  # fallback：长序列拉不到时退回 365d 短序列
        _wl_dates = brk.get("dates") or []
        _wl_values = whales_pct.get("series") or []
    _ahr_price_by_date = {}
    _ahr_all = data.get("ahr999") or {}
    for _d, _p in zip(_ahr_all.get("dates") or [], _ahr_all.get("btc_prices") or []):
        _ahr_price_by_date[_d] = _p
    # weekly downsample（每 7d 一点，含末点）
    whales_pct_weekly_labels = []
    whales_pct_weekly_values = []
    whales_pct_btc_price_weekly = []
    n = min(len(_wl_dates), len(_wl_values))
    idxs = list(range(0, n, 7))
    if idxs and idxs[-1] != n - 1:
        idxs.append(n - 1)  # 保证最新一天入图
    for i in idxs:
        if _wl_values[i] is None:
            continue
        whales_pct_weekly_labels.append(_wl_dates[i])
        whales_pct_weekly_values.append(_wl_values[i])
        whales_pct_btc_price_weekly.append(_ahr_price_by_date.get(_wl_dates[i]))

    # ETF 累计净流入（USD，用于 SECTION A 数据列 + 历史 backup）
    etf_daily = etf.get("daily_flows") or []
    etf_cum_values = []
    if etf_daily:
        acc = 0
        for row in etf_daily:
            flow = row.get("total_flow_m") or row.get("net_flow_m") or row.get("flow") or 0
            try:
                acc += float(flow)
            except (TypeError, ValueError):
                pass
            etf_cum_values.append(round(acc, 1))

    # ETF 累计持有 BTC 数量（dashboard 唯一保留图，PPT 简化版）：
    # 每日 USD flow / 当日 BTC 价格 → 当日净增 BTC → 累加
    # BTC 价格序列来自 ahr999.json（dates + btc_prices, ≥ 2016 起 daily）
    ahr = data.get("ahr999", {}) or {}
    ahr_dates = ahr.get("dates") or []
    ahr_prices = ahr.get("btc_prices") or []
    price_by_date = {ahr_dates[i]: ahr_prices[i] for i in range(min(len(ahr_dates), len(ahr_prices))) if ahr_prices[i]}

    etf_btc_labels = []
    etf_cum_btc = []
    etf_btc_price = []
    if etf_daily and price_by_date:
        cum_btc = 0.0
        for row in etf_daily:
            date_str = (row.get("date") or "")[:10]
            if not date_str:
                continue
            price = price_by_date.get(date_str)
            if not price:
                # 兜底：找最近一天的价（向前回退最多 7 天）
                from datetime import datetime as _dt, timedelta as _td
                try:
                    d_obj = _dt.strptime(date_str, "%Y-%m-%d")
                    for back in range(1, 8):
                        prev = (d_obj - _td(days=back)).strftime("%Y-%m-%d")
                        if prev in price_by_date:
                            price = price_by_date[prev]
                            break
                except Exception:
                    pass
            if not price:
                continue
            flow_m = row.get("total_flow_m") or 0
            try:
                btc_delta = (float(flow_m) * 1e6) / float(price)
            except (TypeError, ValueError, ZeroDivisionError):
                btc_delta = 0
            cum_btc += btc_delta
            etf_btc_labels.append(date_str)
            etf_cum_btc.append(round(cum_btc, 0))
            etf_btc_price.append(round(float(price), 0))

    # ETF 周度净流入（PPT slide 7 要求"拆成只有 ETF flow，需要改成周度频率"）
    etf_weekly_labels = []
    etf_weekly_values = []
    if etf_daily:
        from datetime import datetime as _dt
        weekly_bucket = {}
        for row in etf_daily:
            date_str = row.get("date") or row.get("Date")
            if not date_str:
                continue
            try:
                d = _dt.strptime(str(date_str)[:10], "%Y-%m-%d")
            except Exception:
                continue
            # 用 ISO 周 (year, week) 作 key
            iso_year, iso_week, _ = d.isocalendar()
            wkey = f"{iso_year}-W{iso_week:02d}"
            flow = row.get("total_flow_m") or row.get("net_flow_m") or row.get("flow") or 0
            try:
                weekly_bucket[wkey] = weekly_bucket.get(wkey, 0) + float(flow)
            except (TypeError, ValueError):
                pass
        for wkey in sorted(weekly_bucket.keys()):
            etf_weekly_labels.append(wkey)
            etf_weekly_values.append(round(weekly_bucket[wkey], 1))

    # 因素 5 整体资金流向：AI 看最新 CoinShares 图后，在 extra 中写入视觉估算。
    # 不再拿 Farside ETF 周流量冒充 CoinShares 全市场产品流分位。
    total_flow_percentile = None
    total_flow_label = None
    flow_estimate = extra.get("coinshares_flow_percentile") or {}
    try:
        estimated_value = float(flow_estimate.get("value"))
        if 0 <= estimated_value <= 100 and flow_estimate.get("method") == "visual_estimate":
            total_flow_percentile = estimated_value
            total_flow_label = flow_estimate.get("label") or "中性"
    except (TypeError, ValueError):
        pass

    # 因素 8 验证 5：稳定币状态标签
    sc_30d = stable.get("change_30d") or 0
    if sc_30d >= 3:
        stable_status_label, stable_status_color = "快速发展", "#dcfce7"
    elif sc_30d >= 0.5:
        stable_status_label, stable_status_color = "缓慢发展", "#fef9c3"
    elif sc_30d >= -0.5:
        stable_status_label, stable_status_color = "停滞", "#f3f4f6"
    else:
        stable_status_label, stable_status_color = "下降", "#fee2e2"

    # 因素 6 图 6 OI 下半显示门控：2026-07-03 撤销 11/1 时间门控（Coinalyze 4 年聚合已就位），改为数据驱动
    _dv = data.get("derivatives_data", {}) or {}
    _oi_n = len(((_dv.get("coinalyze_oi_agg") or {}).get("dates")) or (_dv.get("market_aggregate_history") or []))
    _show_oi_chart = _oi_n >= 30

    def _corr_signed(key):
        value = corr.get(key)
        value = 0.0 if value is not None and abs(value) < 0.005 else (value or 0.0)
        return f"{value:+.2f}"

    ctx = {
        "report_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "show_oi_chart": _show_oi_chart,

        # Header
        "btc_current": btc.get("current", 0),
        "btc_change_7d": btc.get("change_7d"),
        "btc_change_30d": btc.get("change_30d"),

        # SECTION A 本期总结（PPT slide 1）
        "summary_rows": build_summary_rows(data, extra),

        # 因素一览（机械指标速览）
        "factor_rows": build_factor_rows(data, extra),

        # Correlations
        "corr_btc_gold_3m": corr.get("btc_gold_3m"),
        "corr_btc_gold_6m": corr.get("btc_gold_6m"),
        "corr_btc_sox_3m": corr.get("btc_sox_3m"),
        "corr_btc_sox_6m": corr.get("btc_sox_6m"),

        # 因素 1 四年周期 PPT 要求不放 detailed-data；ctx 不再暴露 cycle 字段

        # 因素 2 DAT
        "public_btc": tres.get("totals", {}).get("public_btc", 0),
        "private_btc": tres.get("totals", {}).get("private_btc", 0),
        "key_companies": prepare_key_companies(tres),
        "flows": tres.get("flows", {}),
        # PPT 4.5 "仅保留最近一个月"，模板渲染前过滤过期项
        "dat_announcements": _filter_recent_announcements(
            extra.get("dat_announcements") or user.get("dat_announcements") or [],
            days=30,
        ),

        # 因素 3 ETF
        "etf_sum_7d": etf_summary.get("sum_7d_m"),
        "etf_sum_30d": etf_summary.get("sum_30d_m"),
        "etf_latest_date": etf_summary.get("latest_date", "—"),
        "etf_latest_flow": etf_summary.get("latest_flow_m"),
        "etf_pos_days": etf_summary.get("positive_days_30d", 0),
        "etf_neg_days": etf_summary.get("negative_days_30d", 0),
        "chart_etf_cum_values": json.dumps(etf_cum_values),
        "chart_etf_weekly_labels": json.dumps(etf_weekly_labels),
        "chart_etf_weekly_values": json.dumps(etf_weekly_values),
        # 唯一保留图：ETF 累计持有 BTC 数量 + BTC 价格双轴（2024-01-11 起 daily）
        "chart_etf_btc_labels": json.dumps(etf_btc_labels),
        "chart_etf_btc_holdings": json.dumps(etf_cum_btc),
        "chart_etf_btc_price": json.dumps(etf_btc_price),

        # PPT 图嵌入（base64 data URI，从 data/ppt_assets/ 加载）
        # 用于占位"找不到数据源"的图：DAT 合计 flow / DAT 周度 / Whales% / CoinShares 资金 / F&G / ahr999
        "ppt_img_dat_flow": _load_ppt_asset("dat_flow"),
        "ppt_img_dat_per_company": _load_ppt_asset("dat_per_company"),
        "ppt_img_whales_pct": _load_ppt_asset("whales_pct"),
        "ppt_img_coinshares_flow": _load_ppt_asset("coinshares_flow"),
        "ppt_img_fear_greed": _load_ppt_asset("fear_greed"),
        "ppt_img_ahr999": _load_ppt_asset("ahr999"),
        "ppt_img_mnav": _load_ppt_asset("mnav"),
        # 实时 Coinglass 截图（每日 cron 跑 take_screenshots.py 更新）
        "shot_coinglass_ahr999": _load_screenshot("coinglass_ahr999"),

        # 因素 4 Whales（一句话 AI 自动生成，按 PPT slide 8 要求"基于本 section 数据找最近 whales 明显买入/卖出"）
        # whale_sections 字段 2026-05-20 删除——模板不渲染（line 572 注释说明），内容已由 analyst_funding_cards
        # 的 whale_summary 段 + whale_news_summary 字段（从策略组 ① 段自动拼接）覆盖。extra JSON 仍可传但被忽略。
        # whale_buysell_news：知名鲸鱼买卖新闻（最近 2 周内，每条 1 句），独立 WebSearch 抓取（区别于策略组 ① 段拼接）
        "whale_buysell_news": _filter_recent_announcements(
            extra.get("whale_buysell_news", []), days=14
        ),
        # 🟢 extra.whale_one_liner = 唯一合法 AI 手写注入点（详见 build_summary_rows 同名注释）
        "whale_summary": (
            extra.get("whale_one_liner")
            or user.get("whale_summary")
            or _whale_one_liner_ai(api, brk, whales_pct)
        ),
        "whale_news": extra.get("whale_news") or user.get("whale_news") or [],
        "whales_pct_current": whales_pct.get("current"),
        "whales_pct_30d_change": whales_pct.get("change_30d"),
        # Strategy 官方周度净买入 + 跨公司统一 EV mNAV（2026-06-26 后同口径）
        "chart_strat_weekly_labels": json.dumps(_strat_weekly.get("week_start") or []),
        "chart_strat_weekly_values": json.dumps(_strat_weekly.get("net_btc") or []),
        "chart_ev_mnav_labels": json.dumps(_mnav_dates),
        "chart_ev_mnav_mstr": json.dumps(_mnav_series.get("MSTR") or []),
        "chart_ev_mnav_xxi": json.dumps(_mnav_series.get("XXI") or []),
        "chart_ev_mnav_metaplanet": json.dumps(_mnav_series.get("MPJPY") or []),
        "ev_mnav_current": _mnav_current,
        "ev_mnav_methodology": _mnav_methodology,
        "strat_holdings_latest": ((_strat.get("purchases") or [{}])[-1].get("btc_holdings")),
        "strat_as_of": (_strat.get("sec_reconciliation") or {}).get("latest_as_of", "未核实"),
        "strat_source_url": (
            "https://www.sec.gov/Archives/edgar/data/1050446/" +
            _strat["sec_reconciliation"]["latest_accession"].replace("-", "") + "/" +
            _strat["sec_reconciliation"]["latest_accession"] + "-index.html"
            if (_strat.get("sec_reconciliation") or {}).get("latest_accession")
            else "https://www.strategy.com/ledger"),
        "strat_source_label": "SEC 官方最新持仓披露" if _strat.get("sec_reconciliation") else "Strategy 官方账本",
        "chart_tether_quarterly_labels": json.dumps([row["quarter"] for row in _tether_quarterly_history]),
        "chart_tether_quarterly_btc": json.dumps([round(row["btc_count"], 2) for row in _tether_quarterly_history]),
        "chart_whales_pct_labels": json.dumps(whales_pct_weekly_labels),
        "chart_whales_pct_values": json.dumps(whales_pct_weekly_values),
        "chart_whales_pct_btc_price": json.dumps(whales_pct_btc_price_weekly),
        # PPT slide 9 要求：Exchange Reserve 真实数据
        # 主源 = Coin Metrics Community SplyExNtv（17 年 daily，时序图）
        # 辅源 = Coinglass Balance（22 家分项当前快照 + 24h/7d/30d 变化）
        "exchange_reserve": data.get("exchange_reserve", {}),
        "exchange_reserve_signal": (data.get("exchange_reserve", {}) or {}).get("signal") or extra.get("exchange_reserve_signal") or _exchange_reserve_signal(brk),
        # Coin Metrics 长时序（PPT 要求 ≥ 3 年最佳）—— 周度采样压缩到 ~900 点
        **_build_cm_exchange_reserve_chart(data.get("coinmetrics_data", {}), data.get("ahr999", {})),
        "onchain_summary_cn": extra.get("onchain_summary_cn") or [],
        # PPT slide 10 因素 5：CoinShares 周报截图（5 张 PNG base64 嵌入）
        "coinshares_images": (data.get("coinshares_data", {}) or {}).get("images") or [],

        # 因素 5 整体资金流向
        "total_flow_percentile": total_flow_percentile,
        "total_flow_label": total_flow_label,

        # PPT 4.4 SSOT：DAT section 一句话总结（与 SECTION A 行 3 同源）
        "dat_one_liner": _dat_one_liner(data, extra),
        # PPT 8.3 Whales 新闻汇总（用户填或 AI 合成）
        "whale_news_summary": extra.get("whale_news_summary") or _whale_news_summary(extra),
        # PPT 7.2 SSOT：ETF section 一句话总结（与 SECTION A 行 4 同源）
        "etf_one_liner": _etf_one_liner(data, extra),

        # 因素 8 美国政府和主流机构态度（5 验证问题）
        # PPT 14.1 SSOT：政府 section 一句话总结（与 SECTION A 行 7 同源）
        # AI override (extra.policy_one_liner) 已删除 2026-05-21——只走 user_inputs 或 helper
        "policy_one_liner": user.get("legislation_summary") or _policy_one_liner(data, extra),
        # PPT 14.6 验证 5 稳定币（图驱动，按 PPT 范例格式）
        # 2026-05-20 修复：(a) change_1y 是 None 时不显示该段；(b) stable_status_label 已含"发展"二字，不再尾缀"发展"
        "verify_5_stablecoin_text": (
            f"稳定币市值 USD {(stable.get('total_mcap') or 0) / 1e9:.0f}bn"
            + (
                f"，比 1 年前{'+' if stable['change_1y'] >= 0 else ''}{stable['change_1y']}%"
                if stable.get("change_1y") is not None else ""
            )
            + f"，{stable_status_label}"
        ) if stable.get("total_mcap") else None,
        "verify_1_strategic_reserve": extra.get("verify_1_strategic_reserve") or user.get("verify_1_strategic_reserve"),
        "verify_2_trump_clearance": extra.get("verify_2_trump_clearance") or user.get("verify_2_trump_clearance"),
        "verify_3_institutional_entry": extra.get("verify_3_institutional_entry") or user.get("verify_3_institutional_entry"),
        "verify_4_clarity_act": extra.get("verify_4_clarity_act") or user.get("verify_4_clarity_act"),
        "verify_4_clarity_passage_probability": extra.get("verify_4_clarity_passage_probability"),
        "stable_status_label": stable_status_label,
        "stable_status_color": stable_status_color,

        # WebSearch freshness 标记（cache vs fresh），模板用来决定是否显示"未本期 WebSearch · 上次 X 结果"badge
        "websearch_freshness": extra.get("_websearch_freshness", {}),
        # 用户填的 4 项 freshness 标记（cache vs fresh）
        "user_inputs_freshness": extra.get("_user_inputs_freshness", {}),

        # SECTION 3 观点跟踪一句话总结 — PPT 16.1 SSOT（与 SECTION A 行 8 同源）
        # extra.opinion_one_liner 仅来自 SSOT load_extra line 195-200 自动 set（AI stdin 直传已在 load 时被 pop）
        "opinion_one_liner": extra.get("opinion_one_liner") or user.get("opinion_summary") or _opinions_one_liner(extra),

        # PPT 1.2 / slide 2 — 宏观相关性 AI helper（SECTION A 行 1 第 2 段 + slide 2 双图 caption 共用）
        "macro_correlation_ai": _macro_correlation_one_liner_ai(api),
        # PPT 2.2+2.3 — slide 2 黄金 / AI 资产 双图 caption（AI 根据各图数据）
        "macro_gold_caption_ai": (
            f"比特币与黄金近 3 个月日收益率相关性 {_corr_signed('btc_gold_3m')}，6 个月 {_corr_signed('btc_gold_6m')}。"
            + ("近期回到正相关、比特币强于黄金" if corr.get('btc_gold_3m', 0) > 0.2 else "近期处于负相关或弱相关")
        ),
        "macro_sox_caption_ai": (
            f"比特币与 AI 资产近 3 个月日收益率相关性 {_corr_signed('btc_sox_3m')}，6 个月 {_corr_signed('btc_sox_6m')}。"
            + ("近期回到正相关、比特币 " + ("强于" if abs(corr.get('btc_sox_3m', 0)) > abs(corr.get('btc_gold_3m', 0)) else "弱于") + " AI 资产" if corr.get('btc_sox_3m', 0) > 0.2 else "近期处于负相关或弱相关")
        ),

        # Funding
        "funding_source": funding.get("source", "Hyperliquid"),
        "funding_current": funding.get("current_rate_pct", 0),
        "funding_percentile": fp,
        "funding_pct_color": pct_color(fp),
        "negative_streaks": (funding.get("negative_streak", []) or [])[-1:],
        "oi_current": funding.get("current_oi_usd"),
        "mark_price": funding.get("mark_price"),

        # 情绪指标（因素 6）
        "fear_greed_current": fg.get("current"),
        "fear_greed_label": fg.get("classification_zh") or fg.get("classification"),
        "fear_greed_color": _fg_color(fg.get("current")),
        "fear_greed_chart_labels": json.dumps(fg.get("dates", [])),
        "fear_greed_chart_values": json.dumps(fg.get("values", [])),
        "ahr999_current": ahr.get("current"),
        "ahr999_color": _ahr999_color(ahr.get("current")),
        "ahr999_chart_labels": json.dumps(ahr.get("dates", [])),
        "ahr999_chart_values": json.dumps(ahr.get("values", [])),
        "ahr999_price_values": json.dumps(ahr.get("btc_prices", [])),
        "ahr999_threshold_low": AHR999_BOTTOM,
        "ahr999_threshold_high": AHR999_TOP,
        # 因素 6 图 4：CVDD（Cumulative Value Coin Days Destroyed，大周期底部参考线）
        **_build_cvdd_chart(data),
        # 因素 6 图 5：MVRV Z-Score（<0 底 / ≥4.0 顶）
        **_build_mvrv_z_chart(data),
        "sentiment_signal": _sentiment_signal_text(
            fg.get("current"),
            ahr.get("current"),
            funding,
            _cvdd_state_from_data(data),
            ((data.get("bgeometrics_data") or {}).get("mvrv_zscore") or {}).get("current"),
        ),

        # Stablecoin
        "stable_total": stable.get("total_mcap"),
        "stable_usdt": stable.get("usdt_mcap"),
        "stable_usdc": stable.get("usdc_mcap"),
        "stable_change_7d": stable.get("change_7d"),
        "stable_change_30d": stable.get("change_30d"),

        # News (policy 9/10 + AI-curated overrides)
        "news_items": _policy_news_from_data(policy, extra.get("news", [])),

        # Legislation — prefer manual override, else auto-derived from Congress.gov actions
        "legislation_milestones": extra.get("legislation_milestones") or _clarity_milestones_from_policy(policy),
        "legislation_summary": user.get("legislation_summary"),
        "clarity_bill_url": (policy.get("factor_11_clarity_act") or {}).get("url"),
        "clarity_total_actions": (policy.get("factor_11_clarity_act") or {}).get("total_actions"),

        # Session 3 — new data sources
        "brk_latest": brk.get("latest", {}),
        "brk_accumulation": brk.get("accumulation", {}),
        "deriv_aggregate": deriv.get("coingecko_aggregate", {}),
        "deriv_okx_history": deriv.get("okx_oi_history", {}),
        "mnav_snapshot": mnav.get("snapshot", [])[:20],
        "mnav_count": mnav.get("count", 0),
        "dat_cost_basis": dat.get("cost_basis_summary", [])[:15],
        "dat_total_holdings": dat.get("total_holdings"),
        "dat_total_value_usd": dat.get("total_value_usd"),
        "dat_market_cap_dominance_pct": dat.get("market_cap_dominance_pct"),
        "dat_history_days": dat.get("history_days_accumulated", 0),
        "dat_total_cost": dat.get("total_cost_summary", {}),
        "tether_holdings": (tether.get("current_holdings") or {}),
        "coinshares_paras": coinshares.get("summary_paragraphs", []),
        "coinshares_title": coinshares.get("title"),
        "coinshares_url": coinshares.get("url"),
        "coinshares_report_date": coinshares.get("report_date"),
        # Arkham is in 资金面分析 group per user's original list but functionally
        # belongs in 鲸鱼链上 visual section — split on name not group.
        "kol_tweets": [r for r in tweets.get("results", []) if r.get("name") not in {"Lookonchain", "Whale Alert", "Arkham"}],
        "onchain_tweets": [r for r in tweets.get("results", []) if r.get("name") in {"Lookonchain", "Whale Alert", "Arkham"}],
        "screenshots": load_screenshots(),
        # Latest official quarterly BDO attestation, dynamically parsed by fetch_tether.py.
        "tether_quarterly": tether.get("quarterly_reserves"),
        "kol_success_rate": (tweets.get("successful", 0), tweets.get("total_handles", 0)),

        # Opinions
        "has_opinions": has_opinions,
        "opinion_categories": opinion_categories,
        "analyst_funding_cards": analyst_funding_cards,
    }

    ctx.update(prepare_chart_data(data))
    return ctx


# ── Render + Output ──────────────────────────────────────────

def render(ctx):
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
    )
    env.globals["fmt_num"] = fmt_num
    env.globals["fmt_usd"] = fmt_usd
    template = env.get_template("report.html")
    return template.render(**ctx)


def main():
    data = load_data()
    extra = load_extra_input()

    # 跑过 fresh WebSearch / 用户输入的字段写回 cache（自动覆盖 seed/上一版）
    save_websearch_cache(extra)
    save_user_inputs_cache(extra)

    ctx = build_context(data, extra)
    html = render(ctx)

    # A manual full run refreshes the dated editorial snapshot for cloud reuse.
    # cloud_refresh imports build_context directly and never enters this path.
    publication_config = PROJECT_DIR / "publication" / "config.json"
    if publication_config.exists():
        config = json.loads(publication_config.read_text())
        snapshot = {
            "as_of": datetime.now().strftime("%Y-%m-%d"),
            "context": {key: ctx.get(key) for key in config["editorial_keys"]},
        }
        (PROJECT_DIR / "publication" / "editorial.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
        )

    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = OUTPUT_BASE / today
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / "btc-tracking-report.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Report saved to {out_path}")

    latest = OUTPUT_BASE / "latest.html"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(out_path)
    print(f"Symlink: {latest} -> {out_path}")

    return str(out_path)


if __name__ == "__main__":
    main()
