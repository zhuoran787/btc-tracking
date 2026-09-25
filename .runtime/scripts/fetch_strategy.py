"""Fetch Strategy (MSTR) official BTC transaction history.

Primary source:
- Strategy ledger ``__NEXT_DATA__`` for the complete history since 2020.

Automatic fallback / reconciliation:
- SEC EDGAR submissions API to locate Strategy's recent 8-K filings.
- SEC complete-submission text to parse the official ``BTC Update`` table.

If the Strategy ledger is blocked and SEC reconciliation also fails, the last
valid output file is left byte-for-byte unchanged.  A transient network failure
must never erase the historical purchase ledger.

The former self-computed equity-market-cap / gross-BTC-NAV series remains
deprecated.  Comparable mNAV comes only from ``fetch_mnav.py``.

Output: data/strategy_data.json
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "strategy_data.json"

STRATEGY_URLS = (
    "https://www.strategy.com/ledger",
    "https://www.strategy.com/purchases",
)
STRATEGY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

SEC_CIK = "0001050446"
SEC_CIK_PATH = str(int(SEC_CIK))
SEC_SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{SEC_CIK}.json"
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "btc-tracking research zhuoranwang/0.2 (personal use)",
)
SEC_HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}


def _find_bitcoin_data(obj) -> list[dict] | None:
    """Find the ledger array even if Strategy moves it inside NEXT_DATA."""
    if isinstance(obj, dict):
        direct = obj.get("bitcoinData")
        if isinstance(direct, list) and len(direct) >= 80:
            return direct
        for value in obj.values():
            found = _find_bitcoin_data(value)
            if found is not None:
                return found
    elif isinstance(obj, list) and len(obj) >= 80:
        sample = [x for x in obj[:10] if isinstance(x, dict)]
        if sample and any(
            {"date_of_purchase", "count", "btc_holdings"}.issubset(x)
            for x in sample
        ):
            return obj
    return None


def fetch_purchases() -> tuple[list[dict], str]:
    """Fetch Strategy's complete official ledger from either known URL."""
    errors = []
    for url in STRATEGY_URLS:
        try:
            response = requests.get(url, headers=STRATEGY_HEADERS, timeout=30)
            response.raise_for_status()
            match = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                response.text,
                re.S,
            )
            if not match:
                raise ValueError("__NEXT_DATA__ not found")
            raw = _find_bitcoin_data(json.loads(match.group(1)))
            if raw is None:
                raise ValueError("bitcoinData array not found or too short")

            rows = []
            for item in raw:
                purchase_date = item.get("date_of_purchase")
                if not purchase_date:
                    continue
                rows.append({
                    "date": purchase_date[:10],
                    "btc_delta": item.get("count"),
                    "btc_holdings": item.get("btc_holdings"),
                    "avg_price": item.get("average_price"),
                    "total_cost": item.get("total_acquisition_cost"),
                    "shares_basic": item.get("basic_shares_outstanding"),
                    "shares_diluted": item.get("assumed_diluted_shares_outstanding"),
                })
            rows.sort(key=lambda row: row["date"])
            if len(rows) < 80 or not rows[-1].get("btc_holdings"):
                raise ValueError(f"transformed ledger invalid ({len(rows)} rows)")
            return rows, url
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors))


def weekly_net_buys(purchases: list[dict], start: str = "2024-01-01") -> dict:
    """Weekly net BTC purchases, bucketed to ISO Monday."""
    from collections import defaultdict

    buckets = defaultdict(float)
    for purchase in purchases:
        if purchase["date"] < start or not purchase["btc_delta"]:
            continue
        purchase_date = date.fromisoformat(purchase["date"])
        week_start = purchase_date - timedelta(days=purchase_date.weekday())
        buckets[week_start.isoformat()] += float(purchase["btc_delta"])
    weeks = sorted(buckets)
    return {"week_start": weeks, "net_btc": [round(buckets[w], 2) for w in weeks]}


def _load_last_good() -> dict | None:
    if not OUT.exists():
        return None
    try:
        old = json.loads(OUT.read_text())
        purchases = old.get("purchases") or []
        if len(purchases) < 80 or not purchases[-1].get("btc_holdings"):
            return None
        return old
    except Exception:
        return None


def _parse_sec_date(value: str) -> str:
    cleaned = value.strip().rstrip("*")
    return datetime.strptime(cleaned, "%B %d, %Y").date().isoformat()


def _numeric_cell(value: str) -> float | None:
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    if cleaned.replace(" ", "") == "-":
        return 0.0
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    number = float(match.group(0))
    if cleaned.startswith("(") and not cleaned.startswith("(1)"):
        number = -abs(number)
    return number


def _row_values(row: list[str]) -> list[float]:
    values = []
    for cell in row:
        value = _numeric_cell(cell)
        if value is not None:
            values.append(value)
    return values


def _extract_8k_html(complete_submission: str) -> str:
    for match in re.finditer(r"<DOCUMENT>(.*?)</DOCUMENT>", complete_submission, re.I | re.S):
        document = match.group(1)
        if not re.search(r"<TYPE>8-K(?:\s|$)", document, re.I):
            continue
        text_match = re.search(r"<TEXT>(.*?)</TEXT>", document, re.I | re.S)
        return text_match.group(1) if text_match else document
    raise ValueError("8-K document not found in complete submission")


def parse_sec_btc_events(complete_submission: str, filing_date: str, accession: str) -> list[dict]:
    """Parse one or more official BTC Update table groups from an 8-K."""
    html = _extract_8k_html(complete_submission)
    soup = BeautifulSoup(html, "html.parser")
    plain_text = " ".join(soup.stripped_strings)
    if "BTC Update" not in plain_text and "BTC Updates" not in plain_text:
        return []

    events = []
    for table in soup.find_all("table"):
        table_text = " ".join(table.stripped_strings)
        if "Aggregate BTC Holdings" not in table_text:
            continue
        rows = [
            [" ".join(cell.stripped_strings) for cell in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
        ]
        period_indexes = [
            i for i, row in enumerate(rows) if "During Period" in " ".join(row)
        ]
        for group_number, start_index in enumerate(period_indexes):
            end_index = (
                period_indexes[group_number + 1]
                if group_number + 1 < len(period_indexes)
                else len(rows)
            )
            group = rows[start_index:end_index]
            group_text = " ".join(" ".join(row) for row in group)

            as_of_match = re.search(
                r"As of ([A-Z][a-z]+ \d{1,2}, \d{4})\*?",
                group_text,
            )
            if not as_of_match:
                continue
            as_of = _parse_sec_date(as_of_match.group(1))

            activity_header_index = None
            activity_label = ""
            for i, row in enumerate(group):
                joined = " ".join(row)
                if re.search(r"BTC (?:Acquired|Purchased|Sold)", joined, re.I):
                    activity_header_index = i
                    activity_label = joined
                    break
            if activity_header_index is None:
                continue

            activity_values = []
            for row in group[activity_header_index + 1:]:
                activity_values = _row_values(row)
                if activity_values:
                    break
            if not activity_values:
                continue

            if len(activity_values) >= 6:
                quantity = activity_values[0]
                holdings, total_cost_billions, average_cost = activity_values[-3:]
            else:
                quantity = activity_values[0]
                holdings_values = []
                for i, row in enumerate(group):
                    if "Aggregate BTC Holdings" not in " ".join(row):
                        continue
                    for value_row in group[i + 1:]:
                        holdings_values = _row_values(value_row)
                        if len(holdings_values) >= 3:
                            break
                    if holdings_values:
                        break
                if len(holdings_values) < 3:
                    continue
                holdings, total_cost_billions, average_cost = holdings_values[:3]

            label_lower = activity_label.lower()
            if "btc sold" in label_lower:
                quantity = -abs(quantity)

            events.append({
                "date": filing_date,
                "as_of": as_of,
                "btc_delta": int(quantity) if quantity.is_integer() else quantity,
                "btc_holdings": int(holdings),
                "avg_price": int(average_cost) if average_cost.is_integer() else average_cost,
                "total_cost": round(total_cost_billions * 1000, 2),
                "shares_basic": None,
                "shares_diluted": None,
                "sec_accession": accession,
            })

    if not events:
        # No-activity announcements may disclose holdings in prose, without a
        # BTC table. Require both an explicit no-trading statement and the
        # complete holdings/cost sentence; never infer zero from a missing table.
        btc_section = re.split(r"BTC Updates?", plain_text, maxsplit=1)[-1]
        btc_section = re.split(r"Repurchase Program Updates?", btc_section, maxsplit=1)[0]
        no_activity = re.search(
            r"did not purchase or sell any bitcoin", btc_section, re.I
        )
        holdings_match = re.search(
            r"As of ([A-Z][a-z]+ \d{1,2}, \d{4}),\s+Strategy holds "
            r"approximately ([\d,]+) bitcoin that were acquired at an aggregate "
            r"purchase price of \$([\d,.]+) billion and an average purchase "
            r"price of approximately \$([\d,]+) per bitcoin",
            btc_section,
        )
        if no_activity and holdings_match:
            as_of, holdings, cost_billions, average_cost = holdings_match.groups()
            events.append({
                "date": filing_date,
                "as_of": _parse_sec_date(as_of),
                "btc_delta": 0,
                "btc_holdings": int(holdings.replace(",", "")),
                "avg_price": int(average_cost.replace(",", "")),
                "total_cost": round(float(cost_billions.replace(",", "")) * 1000, 2),
                "shares_basic": None,
                "shares_diluted": None,
                "sec_accession": accession,
            })
        else:
            raise ValueError("BTC Update found but no parseable holdings table or no-activity disclosure")

    events.sort(key=lambda event: event["as_of"])
    if len(events) > 1:
        for event in events[:-1]:
            event["date"] = event["as_of"]
    return events


def fetch_sec_updates(since: str, previous: dict | None = None) -> tuple[list[dict], dict]:
    """Fetch and parse Strategy 8-K BTC updates filed on or after ``since``."""
    response = requests.get(SEC_SUBMISSIONS_URL, headers=SEC_HEADERS, timeout=30)
    response.raise_for_status()
    recent = response.json()["filings"]["recent"]
    filings = []
    verified = set((previous or {}).get('checked_accessions', []))
    if previous and previous.get('latest_accession'):
        verified.add(previous['latest_accession'])
    for index, form in enumerate(recent["form"]):
        filing_date = recent["filingDate"][index]
        items = recent["items"][index]
        if form != "8-K" or filing_date < since or "7.01" not in items:
            continue
        if recent['accessionNumber'][index] in verified:
            continue
        filings.append({
            "filing_date": filing_date,
            "accession": recent["accessionNumber"][index],
        })
    filings.sort(key=lambda filing: filing["filing_date"])
    if not filings and not previous:
        raise ValueError(f"no Strategy 8-K filings found since {since}")

    events = []
    checked = 0
    for filing in filings:
        accession = filing["accession"]
        accession_path = accession.replace("-", "")
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{SEC_CIK_PATH}/"
            f"{accession_path}/{accession}.txt"
        )
        response = requests.get(url, headers=SEC_HEADERS, timeout=30)
        response.raise_for_status()
        checked += 1
        events.extend(parse_sec_btc_events(
            response.text,
            filing["filing_date"],
            accession,
        ))
        time.sleep(0.12)  # stay below SEC's published 10 requests/second guideline

    if not events and previous:
        metadata = dict(previous)
        metadata.update(filings_checked=checked, btc_updates_found=0,
                        checked_accessions=sorted(verified | {f['accession'] for f in filings}),
                        verified_at=datetime.now().isoformat())
        return [], metadata
    if not events:
        raise ValueError(f"no BTC Update tables found in {checked} Strategy 8-K filings")
    events.sort(key=lambda event: (event["date"], event["as_of"]))
    latest = max(events, key=lambda event: event["as_of"])
    metadata = {
        "source": "SEC EDGAR submissions API + official complete-submission text",
        "cik": SEC_CIK,
        "filings_checked": checked,
        "btc_updates_found": len(events),
        "latest_filing_date": max(event["date"] for event in events),
        "latest_as_of": latest["as_of"],
        "latest_holdings": latest["btc_holdings"],
        "latest_accession": latest["sec_accession"],
        "checked_accessions": sorted(verified | {f['accession'] for f in filings}),
        "verified_at": datetime.now().isoformat(),
    }
    return events, metadata


def reconcile_sec_events(purchases: list[dict], events: list[dict]) -> list[dict]:
    """Validate duplicates and append only new, non-zero SEC transactions."""
    reconciled = [dict(row) for row in purchases]
    by_date = {row["date"]: row for row in reconciled}

    for event in events:
        existing = by_date.get(event["date"])
        if existing:
            holding_gap = abs(float(existing["btc_holdings"]) - event["btc_holdings"])
            delta_gap = abs(float(existing.get("btc_delta") or 0) - event["btc_delta"])
            if holding_gap > 2 or delta_gap > 2:
                raise ValueError(
                    f"SEC mismatch on {event['date']}: "
                    f"ledger delta/holdings={existing.get('btc_delta')}/{existing['btc_holdings']}, "
                    f"SEC={event['btc_delta']}/{event['btc_holdings']}"
                )
            continue

        if event["btc_delta"] == 0:
            latest_before = [row for row in reconciled if row["date"] <= event["date"]]
            if latest_before:
                gap = abs(float(latest_before[-1]["btc_holdings"]) - event["btc_holdings"])
                if gap > 2:
                    raise ValueError(
                        f"SEC no-activity filing {event['date']} reports holdings "
                        f"{event['btc_holdings']}, but ledger has {latest_before[-1]['btc_holdings']}"
                    )
            continue

        if event["date"] < reconciled[-1]["date"]:
            raise ValueError(f"SEC transaction {event['date']} missing inside existing ledger")

        prior_holdings = float(reconciled[-1]["btc_holdings"])
        continuity_gap = abs(prior_holdings + float(event["btc_delta"]) - event["btc_holdings"])
        if continuity_gap > 2:
            raise ValueError(
                f"SEC holdings continuity failed on {event['date']}: "
                f"{prior_holdings} + {event['btc_delta']} != {event['btc_holdings']}"
            )
        clean_event = {key: value for key, value in event.items() if key != "as_of"}
        reconciled.append(clean_event)
        by_date[clean_event["date"]] = clean_event

    reconciled.sort(key=lambda row: row["date"])
    return reconciled


def _atomic_write(result: dict) -> None:
    OUT.parent.mkdir(exist_ok=True)
    temporary = OUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    temporary.replace(OUT)


def main() -> int:
    last_good = _load_last_good()
    warnings = []
    ledger_url = None

    try:
        purchases, ledger_url = fetch_purchases()
        print(
            f"OK Strategy ledger: {len(purchases)} rows "
            f"({purchases[0]['date']} -> {purchases[-1]['date']})",
            file=sys.stderr,
        )
    except Exception as exc:
        warnings.append(f"Strategy ledger unavailable: {exc}")
        if last_good is None:
            print(
                "FAIL fetch_strategy: Strategy ledger unavailable and no valid prior file to preserve",
                file=sys.stderr,
            )
            return 1
        purchases = [dict(row) for row in last_good["purchases"]]
        print(
            f"WARN Strategy ledger unavailable; preserving {len(purchases)} prior rows",
            file=sys.stderr,
        )

    sec_metadata = None
    since = (date.fromisoformat(purchases[-1]["date"]) - timedelta(days=14)).isoformat()
    previous_sec = (last_good or {}).get('sec_reconciliation')
    # Resume only from a validated baseline. Re-reading older, already checked
    # filings made one historical 403 block every subsequent BTC update.
    if previous_sec and purchases[-1]['btc_holdings'] == previous_sec.get('latest_holdings'):
        since = max(since, previous_sec['latest_filing_date'])
    else:
        previous_sec = None
    try:
        sec_events, sec_metadata = fetch_sec_updates(since, previous_sec)
        purchases = reconcile_sec_events(purchases, sec_events)
        if purchases[-1]["btc_holdings"] != sec_metadata["latest_holdings"]:
            raise ValueError(
                f"latest holdings mismatch: ledger={purchases[-1]['btc_holdings']}, "
                f"SEC={sec_metadata['latest_holdings']}"
            )
        print(
            f"OK SEC reconciliation: {sec_metadata['btc_updates_found']} BTC updates, "
            f"latest {sec_metadata['latest_as_of']} holdings={sec_metadata['latest_holdings']:,.0f}",
            file=sys.stderr,
        )
    except Exception as exc:
        if ledger_url is None:
            print(
                f"FAIL fetch_strategy SEC fallback: {exc}; last good file left unchanged",
                file=sys.stderr,
            )
            return 1
        warnings.append(f"SEC reconciliation unavailable: {exc}")
        print(f"WARN SEC reconciliation unavailable: {exc}", file=sys.stderr)

    if ledger_url:
        source = f"{ledger_url} (__NEXT_DATA__, official)"
        if sec_metadata:
            source += " + SEC EDGAR 8-K reconciliation"
    else:
        source = "preserved Strategy official ledger baseline + SEC EDGAR 8-K reconciliation"

    result = {
        "fetched_at": datetime.now().isoformat(),
        "source": source,
        "errors": [],
        "warnings": warnings,
        "purchases": purchases,
        "weekly_net_buys": weekly_net_buys(purchases),
        "mnav": {
            "deprecated": True,
            "reason": "Use fetch_mnav.py EV mNAV; legacy equity/gross-BTC method is not comparable.",
        },
    }
    if sec_metadata:
        result["sec_reconciliation"] = sec_metadata

    _atomic_write(result)
    print(
        f"OK fetch_strategy done: {len(purchases)} rows, warnings={len(warnings)} -> {OUT}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
