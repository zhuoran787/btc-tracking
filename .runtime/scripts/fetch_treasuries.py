"""
fetch_treasuries.py — DAT company BTC holdings from bitcointreasuries.net

Scrapes public companies and private companies pages.
Extracts: holdings, cost basis, changes for key companies.
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_DIR = Path(__file__).parent.parent / "cache"

BASE_URL = "https://bitcointreasuries.net"
URLS = {
    "public": f"{BASE_URL}/public-companies",
    "private": f"{BASE_URL}/private-companies",
    "etfs": f"{BASE_URL}/etfs-and-exchanges",
    "governments": f"{BASE_URL}/governments",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

KEY_PUBLIC_COMPANIES = [
    "Strategy", "MicroStrategy", "Twenty One Capital",
    "MARA", "Marathon", "Metaplanet", "Block", "Tesla", "Coinbase",
]

KEY_PRIVATE_COMPANIES = ["Tether", "Block.one"]


def _fetch_page(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "lxml")


def _parse_number(text):
    """Parse numbers like '1,234', '1.2K', '$1.2B', etc."""
    if not text:
        return None
    text = text.strip().replace(",", "").replace("$", "").replace(" ", "")
    text = text.replace("₿", "").replace("BTC", "")
    # Handle mojibake: ₿ (U+20BF) sometimes appears as â‚¿ (UTF-8 bytes as Latin-1)
    text = text.replace("â\x82¿", "").replace("₿", "")
    text = text.strip()
    if not text or text in ("-", "—", "N/A", "n/a"):
        return None
    multiplier = 1
    if text.upper().endswith("K"):
        multiplier = 1_000
        text = text[:-1]
    elif text.upper().endswith("M"):
        multiplier = 1_000_000
        text = text[:-1]
    elif text.upper().endswith("B"):
        multiplier = 1_000_000_000
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def scrape_table(soup):
    """Extract rows from the main data table on a bitcointreasuries page."""
    tables = soup.find_all("table")
    if not tables:
        return [], []

    main_table = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = main_table.find_all("tr")
    if len(rows) < 2:
        return [], []

    # Headers
    header_cells = rows[0].find_all(["th", "td"])
    headers = [cell.get_text(strip=True).lower() for cell in header_cells]

    # Data rows
    data = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        row_data = {}
        for i, cell in enumerate(cells):
            if i < len(headers):
                row_data[headers[i]] = cell.get_text(strip=True)
        if row_data:
            data.append(row_data)

    return headers, data


def extract_company_data(row_data, headers):
    """Normalize a row into a standard company record.

    bitcointreasuries.net format (as of 2026):
      - "name" column: country flag emoji
      - "bitcoin" column: "CompanyNameTICKER" (name + ticker concatenated)
      - "in usd" column: "₿818,334" (BTC holdings with ₿ prefix)
      - "cost basis", "mnav": masked with * for free tier
    """
    record = {"raw": row_data}

    # Company name: in "bitcoin" column, strip trailing ticker
    bitcoin_col = row_data.get("bitcoin", "")
    name = _clean_company_name(bitcoin_col)
    record["name"] = name if name else row_data.get("name", "Unknown")

    # BTC holdings: in "in usd" column with ₿ prefix
    in_usd = row_data.get("in usd", "")
    btc_val = _parse_number(in_usd)
    if btc_val is not None:
        record["btc_holdings"] = btc_val

    # Market cap
    mcap = _parse_number(row_data.get("market cap", ""))
    if mcap is not None:
        record["market_cap_usd"] = mcap

    # Enterprise value
    ev = _parse_number(row_data.get("enterprise value", ""))
    if ev is not None:
        record["enterprise_value_usd"] = ev

    # Cost basis (often masked with *)
    cost = row_data.get("cost basis", "")
    if cost and "*" not in cost:
        val = _parse_number(cost)
        if val is not None:
            record["avg_cost_usd"] = val

    # mNAV (often masked)
    mnav = row_data.get("mnav", "")
    if mnav and "*" not in mnav and "—" not in mnav:
        mnav_clean = mnav.replace("x", "").strip()
        try:
            record["mnav"] = float(mnav_clean)
        except ValueError:
            pass

    return record


def _clean_company_name(text):
    """Extract company name from 'CompanyNameTICKER' format."""
    if not text:
        return ""
    # Common patterns: "StrategyMSTRBuy", "Twenty One CapitalXXI", "Metaplanet Inc.MPJPY"
    # Remove trailing uppercase ticker (2-5 uppercase letters at end)
    # Also remove "Buy" suffix
    text = re.sub(r'Buy$', '', text)
    text = re.sub(r'[A-Z]{2,6}$', '', text)
    # Clean up trailing dots/spaces
    text = text.rstrip('. ')
    return text


def is_key_company(name, key_list):
    """Check if a company name matches any key company."""
    name_lower = name.lower()
    for key in key_list:
        if key.lower() in name_lower:
            return True
    return False


def compute_flows(current_data, cached_data):
    """Compare current holdings to cached (previous) holdings to estimate flows."""
    if not cached_data:
        return {}

    cached_by_name = {}
    for c in cached_data:
        cached_by_name[c.get("name", "").lower()] = c

    flows = {}
    for company in current_data:
        name = company.get("name", "")
        name_lower = name.lower()
        cached = cached_by_name.get(name_lower)
        if cached and "btc_holdings" in company and "btc_holdings" in cached:
            diff = company["btc_holdings"] - cached["btc_holdings"]
            if diff != 0:
                flows[name] = {
                    "btc_change": diff,
                    "previous": cached["btc_holdings"],
                    "current": company["btc_holdings"],
                }
    return flows


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    output = {
        "fetched_at": datetime.now().isoformat(),
        "public_companies": [],
        "private_companies": [],
        "key_companies": {},
        "totals": {},
        "flows": {},
    }

    # Load previous cache for flow computation
    cache_path = CACHE_DIR / "treasuries_cache.json"
    cached_data = {}
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cached_data = json.load(f)
        except Exception:
            pass

    # Scrape public companies
    print("Fetching public companies...", flush=True)
    try:
        soup = _fetch_page(URLS["public"])
        headers, rows = scrape_table(soup)
        print(f"  Found {len(rows)} public companies, headers: {headers[:5]}")

        total_public_btc = 0
        for row in rows:
            company = extract_company_data(row, headers)
            name = company.get("name", "")
            if name.lower().startswith("total"):
                continue
            output["public_companies"].append(company)
            btc = company.get("btc_holdings", 0) or 0
            total_public_btc += btc

            if is_key_company(name, KEY_PUBLIC_COMPANIES):
                output["key_companies"][name] = company

        output["totals"]["public_btc"] = total_public_btc
        print(f"  Total public BTC: {total_public_btc:,.0f}")

    except Exception as e:
        print(f"  ERROR scraping public companies: {e}")
        output["public_error"] = str(e)

    time.sleep(2)

    # Scrape private companies
    print("Fetching private companies...", flush=True)
    try:
        soup = _fetch_page(URLS["private"])
        headers, rows = scrape_table(soup)
        print(f"  Found {len(rows)} private companies")

        total_private_btc = 0
        for row in rows:
            company = extract_company_data(row, headers)
            name = company.get("name", "")
            if name.lower().startswith("total"):
                continue
            output["private_companies"].append(company)
            btc = company.get("btc_holdings", 0) or 0
            total_private_btc += btc

            if is_key_company(name, KEY_PRIVATE_COMPANIES):
                output["key_companies"][name] = company

        output["totals"]["private_btc"] = total_private_btc
        print(f"  Total private BTC: {total_private_btc:,.0f}")

    except Exception as e:
        print(f"  ERROR scraping private companies: {e}")
        output["private_error"] = str(e)

    # Compute flows vs cache
    all_current = output["public_companies"] + output["private_companies"]
    all_cached = cached_data.get("public_companies", []) + cached_data.get("private_companies", [])
    output["flows"] = compute_flows(all_current, all_cached)
    if output["flows"]:
        print(f"\n  Flows detected (vs last run):")
        for name, flow in output["flows"].items():
            print(f"    {name}: {flow['btc_change']:+,.0f} BTC")

    # Save output
    out_path = DATA_DIR / "treasuries.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nSaved to {out_path}")

    # Update cache for next run
    cache_save = {
        "cached_at": datetime.now().isoformat(),
        "public_companies": output["public_companies"],
        "private_companies": output["private_companies"],
    }
    with open(cache_path, "w") as f:
        json.dump(cache_save, f, indent=2, default=str)
    print(f"Cache updated at {cache_path}")


if __name__ == "__main__":
    main()
