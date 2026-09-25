"""
fetch_etf_flows.py — US BTC Spot ETF daily net flows

Primary: Farside Investors (farside.co.uk/btc/)
Fallback: If Farside fails, outputs empty structure for Claude to fill via WebFetch
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent.parent / "data"

FARSIDE_URL = "https://farside.co.uk/btc/"
FARSIDE_HISTORICAL_URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def fetch_farside_historical_via_playwright():
    """PPT 要求时间跨度 ≥ 1 年。farside /btc/ 主页只有 14 天；historical 子页面有 608 行/28 月。
    主页 Cloudflare 没挡，但 historical 页面挡 → 用 Playwright 通过。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = ctx.new_page()
        page.goto(FARSIDE_HISTORICAL_URL, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(8_000)
        # 找含 > 100 行的大表
        big_table_html = page.evaluate("""
            () => {
                for (const t of document.querySelectorAll('table')) {
                    if (t.querySelectorAll('tr').length > 100) return t.outerHTML;
                }
                return null;
            }
        """)
        browser.close()
    if not big_table_html:
        raise RuntimeError("Farside historical: no large table found")
    return BeautifulSoup(big_table_html, "lxml")


def fetch_farside_etf_flows():
    """优先 Playwright 抓 historical 全表（28 个月），失败回退到 requests 抓主页（14 天）。"""
    try:
        soup = fetch_farside_historical_via_playwright()
        main_table = soup.find("table")
        rows = main_table.find_all("tr")
        if len(rows) > 100:
            return _parse_table_rows(rows)
        # fall through to legacy
    except Exception as e:
        print(f"Playwright historical failed ({e}), falling back to requests /btc/", flush=True)

    # Legacy: requests + main page (14 days only)
    resp = requests.get(FARSIDE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    tables = soup.find_all("table")
    if not tables:
        raise RuntimeError("No tables found on Farside page")

    main_table = None
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) > 10:
            main_table = table
            break

    if not main_table:
        main_table = tables[0]

    rows = main_table.find_all("tr")
    if len(rows) < 3:
        raise RuntimeError(f"Table too small: {len(rows)} rows")
    return _parse_table_rows(rows)


def _parse_table_rows(rows):
    # Parse header row to get ETF names
    header_row = rows[0]
    headers = []
    for th in header_row.find_all(["th", "td"]):
        text = th.get_text(strip=True)
        headers.append(text)

    # Parse data rows
    daily_flows = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        row_data = [cell.get_text(strip=True) for cell in cells]
        date_text = row_data[0] if row_data else ""

        # Skip non-date rows (headers, totals, etc)
        if not date_text or not any(c.isdigit() for c in date_text):
            continue

        # Parse date
        parsed_date = _parse_farside_date(date_text)
        if not parsed_date:
            continue

        # A future/unreported row may have '-' for every fund but a numeric
        # zero in Total. Keep real reported zeroes; ignore that placeholder.
        fund_cells = [value for name, value in zip(headers[1:], row_data[1:])
                      if name.lower() not in ("date", "total", "")]
        if fund_cells and all(value.strip() in ("", "-", "—", "–", "n/a", "N/A")
                              for value in fund_cells):
            continue

        # Parse flow values
        entry = {"date": parsed_date}
        total_flow = 0
        etf_flows = {}

        for i, val_text in enumerate(row_data[1:], 1):
            if i >= len(headers):
                break
            etf_name = headers[i]
            val = _parse_flow_value(val_text)
            if val is not None and etf_name.lower() not in ("date", "total", ""):
                etf_flows[etf_name] = val
            if etf_name.lower() == "total" and val is not None:
                total_flow = val

        # If no "Total" column, sum individual flows
        if total_flow == 0 and etf_flows:
            total_flow = sum(etf_flows.values())

        entry["total_flow_m"] = total_flow
        entry["etf_flows"] = etf_flows
        daily_flows.append(entry)

    return daily_flows, headers


def _parse_farside_date(text):
    """Parse various date formats from Farside table."""
    text = text.strip()
    # Try common formats
    for fmt in ("%d %b %Y", "%d %b", "%d/%m/%Y", "%d/%m", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.year < 2020:
                dt = dt.replace(year=datetime.now().year)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_flow_value(text):
    """Parse a flow value like '123.4', '(45.6)', '-', etc. Returns millions."""
    text = text.strip().replace(",", "").replace("$", "").replace(" ", "")
    if not text or text in ("-", "—", "–", "n/a", "N/A", ""):
        return 0.0

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.startswith("-"):
        negative = True
        text = text[1:]

    try:
        val = float(text)
        return -val if negative else val
    except ValueError:
        return None


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "source": "farside",
        "url": FARSIDE_URL,
        "fetched_at": datetime.now().isoformat(),
        "daily_flows": [],
        "etf_names": [],
        "summary": {},
    }

    try:
        print("Fetching Farside ETF flows...", flush=True)
        daily_flows, headers = fetch_farside_etf_flows()

        output["daily_flows"] = daily_flows
        output["etf_names"] = headers

        if daily_flows:
            # Compute summaries
            all_totals = [d["total_flow_m"] for d in daily_flows if d.get("total_flow_m") is not None]
            recent_7d = all_totals[-7:] if len(all_totals) >= 7 else all_totals
            recent_30d = all_totals[-30:] if len(all_totals) >= 30 else all_totals

            output["summary"] = {
                "total_days": len(daily_flows),
                "latest_date": daily_flows[-1]["date"],
                "latest_flow_m": daily_flows[-1]["total_flow_m"],
                "sum_7d_m": round(sum(recent_7d), 1),
                "sum_30d_m": round(sum(recent_30d), 1),
                "positive_days_30d": sum(1 for v in recent_30d if v > 0),
                "negative_days_30d": sum(1 for v in recent_30d if v < 0),
            }

            print(f"  Got {len(daily_flows)} days of data")
            print(f"  Latest: {output['summary']['latest_date']} = "
                  f"${output['summary']['latest_flow_m']:.1f}M")
            print(f"  7d sum: ${output['summary']['sum_7d_m']:.1f}M")
            print(f"  30d sum: ${output['summary']['sum_30d_m']:.1f}M")

    except Exception as e:
        print(f"ERROR: Farside scrape failed: {e}")
        output["error"] = str(e)
        output["fallback_needed"] = True

    out_path = DATA_DIR / "etf_flows.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    main()
