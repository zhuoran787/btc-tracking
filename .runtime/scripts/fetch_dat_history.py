"""Fetch DAT aggregate totals plus four dynamically sourced company snapshots.

Aggregate market totals remain sourced from CoinGecko's free public-treasury
endpoint.  Holdings and cost basis for the four companies shown in the report
come from their public BitcoinTreasuries.net company pages.  Metaplanet's page
does not publish a USD cost basis, so its native-yen average cost is parsed from
the latest official "Notice of Additional Purchase of Bitcoin" PDF.

There are deliberately no numeric cost-basis fallbacks in this file.  Missing
source data stays missing and is shown as such in the report.
"""
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import fitz
import requests

ROOT = Path(__file__).parent.parent
OUT_JSON = ROOT / "data" / "dat_data.json"
DB = ROOT / "data" / "dat_history.sqlite"
COINGECKO_URL = "https://api.coingecko.com/api/v3/companies/public_treasury/bitcoin"
METAPLANET_DISCLOSURES_URL = "https://metaplanet.jp/en/disclosures"
HEADERS = {"User-Agent": "Mozilla/5.0 btc-tracking/1.0"}

FEATURED_SOURCES = {
    "Strategy": {
        "slug": "strategy", "symbol": "MSTR.US", "country": "USA",
    },
    "Twenty One Capital": {
        "slug": "twenty-one-capital", "symbol": "XXI.US", "country": "USA",
    },
    "Metaplanet": {
        "slug": "metaplanet", "symbol": "3350.T", "country": "Japan",
    },
    "MARA Holdings": {
        "slug": "mara", "symbol": "MARA.US", "country": "USA",
    },
}


def _get(url: str, timeout: int = 30) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response


def fetch_coingecko_aggregate() -> dict:
    return _get(COINGECKO_URL, timeout=20).json()


def fetch_bitcointreasuries_company(name: str, config: dict) -> dict:
    url = f"https://bitcointreasuries.net/public-companies/{config['slug']}"
    html = _get(url).text
    cost_match = re.search(r"cost_basis:(null|[\d.]+)", html)
    holdings_match = re.search(r'holdings:\[\{asset:"BTC",balance:([\d.]+)', html)
    date_match = re.search(r'balance_date:"([^"]+)"', html)
    if not cost_match or not holdings_match or not date_match:
        raise RuntimeError(f"BitcoinTreasuries schema changed for {name}")
    cost_basis_raw = cost_match.group(1)
    holdings_raw = holdings_match.group(1)
    balance_date = date_match.group(1)
    holdings = float(holdings_raw)
    if holdings <= 0:
        raise RuntimeError(f"invalid holdings for {name}: {holdings}")
    total_cost_usd = None if cost_basis_raw == "null" else float(cost_basis_raw)
    if total_cost_usd is not None and total_cost_usd <= 0:
        raise RuntimeError(f"invalid cost basis for {name}: {total_cost_usd}")
    return {
        "name": name,
        "symbol": config["symbol"],
        "country": config["country"],
        "holdings": holdings,
        "holdings_as_of": balance_date,
        "total_cost_basis_usd": total_cost_usd,
        "avg_cost_basis_usd": total_cost_usd / holdings if total_cost_usd else None,
        "holdings_source": url,
        "cost_source": url if total_cost_usd else None,
    }


def fetch_metaplanet_native_cost() -> dict:
    """Parse the latest English official purchase disclosure, not a pinned PDF."""
    html = _get(METAPLANET_DISCLOSURES_URL).text
    records = re.findall(
        r'\\"date\\":\\"([0-9-]+)\\",\\"title\\":\\"Notice of Additional Purchase of Bitcoin[^\"]*\\",'
        r'\\"filePath\\":\\"(https:[^\"]+?\.pdf)\\",\\"isEnglish\\":true',
        html,
        re.I,
    )
    if not records:
        raise RuntimeError("Metaplanet disclosures schema changed or no English purchase PDF found")
    disclosure_date, pdf_url = sorted(records, key=lambda item: item[0], reverse=True)[0]
    pdf = _get(pdf_url, timeout=45).content
    document = fitz.open(stream=pdf, filetype="pdf")
    text = "\n".join(page.get_text() for page in document)
    match = re.search(
        r"Total Bitcoin Holdings.*?([\d,]+)\s+Bitcoin.*?"
        r"Average Purchase Price.*?([\d,]+)\s+yen per Bitcoin.*?"
        r"Aggregate Amount Purchased.*?([\d.]+)\s+billion yen",
        text,
        re.I | re.S,
    )
    if not match:
        raise RuntimeError(f"could not parse Metaplanet cost basis from {pdf_url}")
    holdings, avg_cost_jpy, aggregate_cost_billion_jpy = match.groups()
    as_of_match = re.search(r"As of ([A-Za-z]+ \d{1,2}, 20\d{2}), the Company held", text)
    return {
        "holdings": float(holdings.replace(",", "")),
        "avg_cost_basis_native": float(avg_cost_jpy.replace(",", "")),
        "total_cost_basis_native": float(aggregate_cost_billion_jpy) * 1e9,
        "cost_currency": "JPY",
        "cost_source": pdf_url,
        "cost_as_of": as_of_match.group(1) if as_of_match else disclosure_date,
        "disclosure_date": disclosure_date,
    }


def fetch_featured_companies() -> list[dict]:
    companies = [
        fetch_bitcointreasuries_company(name, config)
        for name, config in FEATURED_SOURCES.items()
    ]
    meta_cost = fetch_metaplanet_native_cost()
    for company in companies:
        if company["name"] != "Metaplanet":
            company["cost_currency"] = "USD"
            company["avg_cost_basis_native"] = company["avg_cost_basis_usd"]
            company["total_cost_basis_native"] = company["total_cost_basis_usd"]
            continue
        if abs(company["holdings"] - meta_cost["holdings"]) > 0.01:
            raise RuntimeError(
                "Metaplanet holdings mismatch: "
                f"BitcoinTreasuries={company['holdings']}, official={meta_cost['holdings']}"
            )
        company.update(meta_cost)
    return companies


def _btc_spot_from_coingecko(snapshot: dict) -> float | None:
    for company in snapshot.get("companies", []):
        holdings = company.get("total_holdings") or 0
        current = company.get("total_current_value_usd") or 0
        if holdings and current:
            return float(current) / float(holdings)
    return None


def persist(companies: list[dict], aggregate: dict, snapshot_date: str) -> dict[str, list[dict]]:
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS dat_snapshots (
            date TEXT,
            name TEXT,
            symbol TEXT,
            country TEXT,
            total_holdings REAL,
            total_entry_value_usd REAL,
            total_current_value_usd REAL,
            pct_of_supply REAL,
            PRIMARY KEY (date, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_dat_symbol_date ON dat_snapshots (symbol, date);
        CREATE TABLE IF NOT EXISTS dat_aggregate (
            date TEXT PRIMARY KEY,
            total_holdings REAL,
            total_value_usd REAL,
            company_count INTEGER
        );
        """
    )
    btc_spot = _btc_spot_from_coingecko(aggregate)
    for company in companies:
        conn.execute(
            "INSERT OR REPLACE INTO dat_snapshots "
            "(date, name, symbol, country, total_holdings, total_entry_value_usd, total_current_value_usd, pct_of_supply) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_date,
                company["name"],
                company["symbol"],
                company["country"],
                company["holdings"],
                company.get("total_cost_basis_usd"),
                company["holdings"] * btc_spot if btc_spot else None,
                company["holdings"] / 21_000_000 * 100,
            ),
        )
    conn.execute(
        "INSERT OR REPLACE INTO dat_aggregate (date, total_holdings, total_value_usd, company_count) VALUES (?, ?, ?, ?)",
        (
            snapshot_date,
            aggregate.get("total_holdings"),
            aggregate.get("total_value_usd"),
            len(aggregate.get("companies", [])),
        ),
    )
    conn.commit()

    history: dict[str, list[dict]] = {}
    for name in FEATURED_SOURCES:
        cur = conn.execute(
            "SELECT date, total_holdings, total_entry_value_usd, total_current_value_usd "
            "FROM dat_snapshots WHERE name=? ORDER BY date",
            (name,),
        )
        history[name] = [
            {"date": d, "holdings": h, "entry_value_usd": ev, "current_value_usd": cv}
            for d, h, ev, cv in cur.fetchall()
        ]
    cur = conn.execute("SELECT date, total_holdings, total_value_usd, company_count FROM dat_aggregate ORDER BY date")
    history["__aggregate__"] = [
        {"date": d, "total_holdings": h, "total_value_usd": v, "company_count": n}
        for d, h, v, n in cur.fetchall()
    ]
    conn.close()
    (ROOT / "data" / "dat_history.json").write_text(json.dumps(history, ensure_ascii=False, indent=2))
    return history


def cost_basis_summary(companies: list[dict], btc_spot: float | None) -> list[dict]:
    out = []
    for company in companies:
        avg_usd = company.get("avg_cost_basis_usd")
        pnl_pct = ((btc_spot / avg_usd - 1) * 100) if btc_spot and avg_usd else None
        out.append({
            **company,
            "avg_current_price_usd": btc_spot,
            "pnl_pct": pnl_pct,
        })
    return out


def total_cost_summary(cost_basis: list[dict]) -> dict:
    tracked_holdings = sum(company.get("holdings") or 0 for company in cost_basis)
    usd_comparable = [company for company in cost_basis if company.get("avg_cost_basis_usd")]
    covered_holdings = sum(company["holdings"] for company in usd_comparable)
    total_entry_usd = sum(company["avg_cost_basis_usd"] * company["holdings"] for company in usd_comparable)
    return {
        "scope": "four tracked companies; USD-comparable rows only",
        "total_entry_value_usd": total_entry_usd,
        "covered_holdings": covered_holdings,
        "tracked_holdings": tracked_holdings,
        "coverage_pct": covered_holdings / tracked_holdings * 100 if tracked_holdings else 0,
        "weighted_avg_cost_usd": total_entry_usd / covered_holdings if covered_holdings else None,
    }


def main() -> int:
    OUT_JSON.parent.mkdir(exist_ok=True)
    today = date.today().isoformat()
    try:
        aggregate = fetch_coingecko_aggregate()
        companies = fetch_featured_companies()
        btc_spot = _btc_spot_from_coingecko(aggregate)
        history = persist(companies, aggregate, today)
        cost_basis = cost_basis_summary(companies, btc_spot)
        total_cost = total_cost_summary(cost_basis)
    except Exception as exc:
        OUT_JSON.write_text(json.dumps({"error": str(exc), "fetched_at": today}, ensure_ascii=False, indent=2))
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    output = {
        "fetched_at": today,
        "aggregate_source": COINGECKO_URL,
        "featured_source": "https://bitcointreasuries.net/public-companies/",
        "featured_companies": list(FEATURED_SOURCES),
        "total_holdings": aggregate.get("total_holdings"),
        "total_value_usd": aggregate.get("total_value_usd"),
        "market_cap_dominance_pct": aggregate.get("market_cap_dominance"),
        "company_count": len(aggregate.get("companies", [])),
        "cost_basis_summary": cost_basis,
        "total_cost_summary": total_cost,
        "history_days_accumulated": len(history.get("__aggregate__", [])),
    }
    OUT_JSON.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(
        f"OK: wrote {OUT_JSON} (tracked={', '.join(output['featured_companies'])}; "
        f"history days={output['history_days_accumulated']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
