"""Fetch comparable DAT-company EV mNAV from BitcoinTreasuries.net.

bitcoinquant.co is fully client-rendered (Next.js) and blocks /api/*. We use
bitcointreasuries.net which has plain HTML tables with mNAV column.

BitcoinTreasuries changed its default methodology on 2026-06-26.  The report
therefore uses only Enterprise Value mNAV snapshots after that change and never
connects them to the legacy basic/fully-diluted observations.

Output:
- data/mnav_data.json        — current snapshot
- data/mnav_history.sqlite   — cumulative daily snapshots
"""
import json
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent.parent
OUT_JSON = ROOT / "data" / "mnav_data.json"
DB = ROOT / "data" / "mnav_history.sqlite"
URL = "https://bitcointreasuries.net/"
METHODOLOGY_URL = "https://bitcointreasuries.net/news/how-bitcointreasuriesnet-calculates-mnav"
METHODOLOGY = "enterprise_value"
METHODOLOGY_VERSION = "bitcointreasuries-ev-2026-06-26"
METHODOLOGY_EFFECTIVE_DATE = "2026-06-26"
FORMULA = "(market_cap + total_debt + preferred_stock - cash) / (btc_holdings * btc_price)"


def num(s: str) -> float | None:
    """Parse '1,234.5' / '[1.04]' / '₿ 818,334' / '$66,326M' / '*.****' -> float or None."""
    if not s:
        return None
    s = s.strip().replace("[", "").replace("]", "").replace(",", "").replace("₿", "").replace("$", "").strip()
    if not s or "*" in s:
        return None
    mult = 1
    if s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("B"):
        mult, s = 1_000_000_000, s[:-1]
    m = re.match(r"^-?[\d.]+$", s)
    if not m:
        return None
    try:
        return float(s) * mult
    except ValueError:
        return None


def scrape_treasuries() -> list[dict]:
    """Return list of {rank, name, ticker, country, btc_holdings, mnav}."""
    r = requests.get(URL, headers={"User-Agent": "Mozilla/5.0 btc-tracking"}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    rows_out: list[dict] = []
    seen_tickers: set[str] = set()

    # Tables 0, 1, 2 have the same 6-column structure: Rank, Name, Flag, Ticker, Bitcoin, [mNAV]
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if not any("mNAV" in h for h in headers):
            continue
        # Only handle the 6-col layout (top 100 list); skip the 12-col masked layout
        if len(headers) != 6 or headers[3] != "Ticker":
            continue

        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if len(cells) < 6:
                continue
            rank = num(cells[0])
            name = cells[1]
            country = cells[2]
            ticker = cells[3]
            btc = num(cells[4])
            mnav = num(cells[5])
            if not ticker or ticker in seen_tickers or mnav is None:
                continue
            seen_tickers.add(ticker)
            rows_out.append({
                "rank": int(rank) if rank else None,
                "name": name,
                "country": country,
                "ticker": ticker,
                "btc_holdings": btc,
                "mnav": mnav,
                "mnav_method": METHODOLOGY,
            })

    rows_out.sort(key=lambda x: x.get("rank") or 9999)
    return rows_out


def persist(rows: list[dict], snapshot_date: str) -> None:
    """Upsert today's snapshot into SQLite. Idempotent if rerun on same day."""
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mnav_snapshots (
            date TEXT,
            ticker TEXT,
            name TEXT,
            btc_holdings REAL,
            mnav REAL,
            method TEXT,
            source_version TEXT,
            source_url TEXT,
            PRIMARY KEY (date, ticker)
        )
        """
    )
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(mnav_snapshots)")}
    for column, definition in (
        ("method", "TEXT"),
        ("source_version", "TEXT"),
        ("source_url", "TEXT"),
    ):
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE mnav_snapshots ADD COLUMN {column} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ticker_date ON mnav_snapshots (ticker, date)")
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO mnav_snapshots "
            "(date, ticker, name, btc_holdings, mnav, method, source_version, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_date, r["ticker"], r["name"], r["btc_holdings"], r["mnav"],
                METHODOLOGY, METHODOLOGY_VERSION, URL,
            ),
        )
    # Existing snapshots on/after the public methodology change came from the
    # same default EV-mNAV column; tag them without rewriting any value.
    conn.execute(
        "UPDATE mnav_snapshots SET method=?, source_version=?, source_url=? "
        "WHERE date>=? AND method IS NULL",
        (METHODOLOGY, METHODOLOGY_VERSION, URL, METHODOLOGY_EFFECTIVE_DATE),
    )
    conn.commit()

    # also build history per ticker for output
    history: dict[str, list[dict]] = {}
    for ticker in {r["ticker"] for r in rows}:
        cur = conn.execute(
            "SELECT date, mnav, btc_holdings FROM mnav_snapshots "
            "WHERE ticker=? AND date>=? AND method=? ORDER BY date",
            (ticker, METHODOLOGY_EFFECTIVE_DATE, METHODOLOGY),
        )
        history[ticker] = [
            {"date": d, "mnav": m, "btc_holdings": b} for d, m, b in cur.fetchall()
        ]
    conn.close()

    # write history file (compact) for the report
    hist_path = ROOT / "data" / "mnav_history.json"
    hist_path.write_text(json.dumps(history, ensure_ascii=False, indent=2))


def main() -> int:
    OUT_JSON.parent.mkdir(exist_ok=True)
    today = date.today().isoformat()

    try:
        rows = scrape_treasuries()
    except Exception as exc:
        OUT_JSON.write_text(json.dumps({"error": str(exc), "fetched_at": today}, ensure_ascii=False, indent=2))
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    persist(rows, today)

    output = {
        "fetched_at": today,
        "source": URL,
        "count": len(rows),
        "snapshot": rows,
        "methodology": {
            "name": METHODOLOGY,
            "version": METHODOLOGY_VERSION,
            "effective_date": METHODOLOGY_EFFECTIVE_DATE,
            "formula": FORMULA,
            "source": METHODOLOGY_URL,
            "scope_note": "Bitcoin miners without explicit Bitcoin KPIs are excluded by the source.",
        },
    }
    OUT_JSON.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"OK: wrote {OUT_JSON} ({len(rows)} companies); SQLite -> {DB}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
