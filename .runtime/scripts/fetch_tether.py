"""Fetch Tether's current BTC holdings and official quarterly attestations.

The latest quarter is discovered from Tether's official news index. Historical
quarters are discovered through Tether's WordPress API and stored separately
from the legacy daily third-party snapshots.  Only attestation PDFs that
disclose both BTC value and the valuation price are used for the BTC-unit
history, so different source/method series are never connected on one chart.

Output: data/tether_data.json
"""
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import fitz
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent.parent
OUT = ROOT / "data" / "tether_data.json"
DB = ROOT / "data" / "tether_history.sqlite"

URL_PRIVATE = "https://bitcointreasuries.net/private-companies"
URL_TRANSPARENCY = "https://tether.to/en/transparency/"
URL_NEWS = "https://tether.io/news/"
URL_WP_POSTS = "https://tether.io/wp-json/wp/v2/posts"
HEADERS = {"User-Agent": "Mozilla/5.0 btc-tracking/1.0"}


def num(s: str) -> float | None:
    """Extract first numeric value from any string (handles unicode glyphs, suffix M/B)."""
    if not s:
        return None
    # match number, optional decimal, with optional commas
    m = re.search(r"-?[\d,]+\.?\d*", s)
    if not m:
        return None
    raw = m.group(0).replace(",", "")
    try:
        v = float(raw)
    except ValueError:
        return None
    if "M" in s:
        return v  # leave M in cell; caller knows the unit
    return v


def fetch_bitcointreasuries() -> dict | None:
    r = requests.get(URL_PRIVATE, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "lxml")
    for tr in soup.find_all("tr"):
        text = tr.get_text(" ", strip=True)
        if "Tether" not in text:
            continue
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if len(cells) < 5:
            continue
        btc = num(cells[3])  # "₿ 97,141"
        usd_m = num(cells[4])  # "$7,868M"
        pct_supply = cells[5] if len(cells) > 5 else None
        return {
            "name": cells[2],
            "country": cells[1],
            "btc_holdings": btc,
            "usd_value_m": usd_m,
            "pct_of_supply": pct_supply,
            "source": URL_PRIVATE,
        }
    return None


def fetch_transparency_links() -> dict:
    """Best-effort: find latest attestation PDF link + any visible reserve hints."""
    try:
        r = requests.get(URL_TRANSPARENCY, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "url": URL_TRANSPARENCY}
        html = r.text
        # try to find any PDF link
        pdfs = re.findall(r'href="([^"]+\.pdf)"', html, re.I)
        attestations = [p for p in pdfs if "attestation" in p.lower() or "reserve" in p.lower()]
        return {
            "url": URL_TRANSPARENCY,
            "latest_attestation_links": attestations[:5] or pdfs[:5],
            "note": "Tether transparency page links; quarterly figures are parsed from the latest official news-linked BDO PDF.",
        }
    except Exception as exc:
        return {"error": str(exc), "url": URL_TRANSPARENCY}


def _money_after(text: str, label_pattern: str) -> int:
    match = re.search(label_pattern + r"(?:\d{1,2})?\s+([\d,]{4,})", text, re.I)
    if not match:
        raise ValueError(f"quarterly PDF field missing: {label_pattern}")
    return int(match.group(1).replace(",", ""))


def _reporting_date(pdf_text: str) -> str:
    patterns = (
        r"Financial Figures and Reserves Report as of (\d{1,2} [A-Za-z]+ 20\d{2})",
        r"Consolidated (?:Financials Figures and )?Reserves Report.*?as (?:of|at) "
        r"(\d{1,2} [A-Za-z]+ 20\d{2})",
        r"\bas (?:of|at) (\d{1,2} [A-Za-z]+ 20\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, pdf_text, re.I | re.S)
        if match:
            return datetime.strptime(match.group(1), "%d %B %Y").strftime("%Y-%m-%d")
    raise RuntimeError("could not identify attestation reporting date")


def _btc_valuation_price(pdf_text: str) -> float | None:
    patterns = (
        r"US\$\s*([\d,.]+)\s*/\s*Bitcoin",
        r"BTC price(?:\s+of)?\s+USD\s*([\d,.]+)",
        r"BTC price(?:\s+of)?\s+US\$\s*([\d,.]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, pdf_text, re.I)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def _quarter_label(as_of: str) -> str:
    d = datetime.strptime(as_of, "%Y-%m-%d")
    return f"{d.year} Q{(d.month - 1) // 3 + 1}"


def _attestation_pdf(article_url: str, article_html: str | None = None) -> tuple[str, str, str]:
    if article_html is None:
        article = requests.get(article_url, headers=HEADERS, timeout=30)
        article.raise_for_status()
        article_html = article.text
    soup = BeautifulSoup(article_html, "lxml")
    article_text = " ".join(soup.get_text(" ", strip=True).split())
    pdf_urls = [a["href"] for a in soup.find_all("a", href=True) if ".pdf" in a["href"].lower()]
    pdf_url = next(
        (u for u in pdf_urls if any(k in u.lower() for k in ("opinion", "isae", "reserve", "tether"))),
        pdf_urls[0] if pdf_urls else None,
    )
    if not pdf_url:
        raise RuntimeError(f"quarterly article has no attestation PDF: {article_url}")
    pdf = requests.get(pdf_url, headers=HEADERS, timeout=45)
    pdf.raise_for_status()
    document = fitz.open(stream=pdf.content, filetype="pdf")
    pdf_text = "\n".join(page.get_text() for page in document)
    return article_text, pdf_url, pdf_text


def parse_quarterly_history_item(article_url: str, article_html: str | None = None) -> dict:
    """Parse one official attestation for a comparable BTC-unit history point."""
    _article_text, pdf_url, pdf_text = _attestation_pdf(article_url, article_html)
    as_of = _reporting_date(pdf_text)
    if as_of < "2023-03-31":
        raise ValueError("BTC reserve category was not separately disclosed before 2023 Q1")
    btc_match = re.search(r"\bBitcoins?(?:\d{1,2})?\s+([\d,]{7,})", pdf_text, re.I)
    if not btc_match:
        raise ValueError("attestation has no separately disclosed BTC reserve value")
    btc_usd = int(btc_match.group(1).replace(",", ""))
    btc_price = _btc_valuation_price(pdf_text)
    if not btc_price:
        raise ValueError("attestation does not disclose its BTC valuation price")
    return {
        "quarter": _quarter_label(as_of),
        "as_of": as_of,
        "btc_usd": btc_usd,
        "btc_count": btc_usd / btc_price,
        "btc_valuation_price_usd": btc_price,
        "btc_count_method": "btc_usd / attestation BTC valuation price",
        "source_article": article_url,
        "source_pdf": pdf_url,
    }


def discover_latest_quarter() -> dict:
    """Discover and parse the latest official Tether quarterly attestation."""
    index = requests.get(URL_NEWS, headers=HEADERS, timeout=30)
    index.raise_for_status()
    soup = BeautifulSoup(index.text, "lxml")
    article_url = None
    article_html = None
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        slug = href.lower()
        if re.search(r"q[1-4]", slug) and any(word in slug for word in ("performance", "profit", "attestation")):
            article_url = href
            break

    # The news index no longer renders older cards as static anchors. Fall back
    # to Tether's official WordPress API, whose results are ordered newest-first.
    if not article_url:
        response = requests.get(
            URL_WP_POSTS,
            params={
                "search": "attestation",
                "per_page": 100,
                "orderby": "date",
                "order": "desc",
            },
            headers=HEADERS,
            timeout=30,
        )
        response.raise_for_status()
        for post in response.json():
            href = post.get("link") or ""
            title = BeautifulSoup(
                (post.get("title") or {}).get("rendered", ""), "lxml"
            ).get_text(" ", strip=True)
            candidate = f"{title} {href}".lower()
            if not href or title.lower().startswith("tether gold"):
                continue
            if re.search(r"q[1-4]", candidate) and any(
                word in candidate for word in ("performance", "profit", "attestation")
            ):
                article_url = href
                article_html = (post.get("content") or {}).get("rendered", "")
                break
    if not article_url:
        raise RuntimeError("could not discover latest quarterly Tether article")

    article_text, pdf_url, pdf_text = _attestation_pdf(article_url, article_html)

    quarter_match = re.search(r"attestation for Q([1-4]) of (20\d{2})", article_text, re.I)
    if not quarter_match:
        quarter_match = re.search(r"\bQ([1-4])\b.*?\b(20\d{2})\b", article_text, re.I)
    if not quarter_match:
        raise RuntimeError("could not identify quarter/year from latest Tether article")
    quarter = f"{quarter_match.group(2)} Q{quarter_match.group(1)}"

    as_of = _reporting_date(pdf_text)

    treasury_bills = _money_after(pdf_text, r"U\.S\. Treasury Bills")
    overnight_repo = _money_after(pdf_text, r"Overnight Reverse Repurchase Agreements")
    term_repo = _money_after(pdf_text, r"Term Reverse Repurchase Agreements")
    gold_usd = _money_after(pdf_text, r"Precious Metals")
    btc_usd = _money_after(pdf_text, r"Bitcoin")
    total_assets = _money_after(pdf_text, r"Total Assets \(1\+2\+3\+4\+5\+6\+7\)")

    btc_price = _btc_valuation_price(pdf_text)
    xau_price_match = re.search(r"US\$\s*([\d,.]+)/Oz", pdf_text, re.I)
    if not btc_price or not xau_price_match:
        raise RuntimeError("could not extract BTC/XAU valuation prices from attestation")
    xau_price = float(xau_price_match.group(1).replace(",", ""))
    btc_count = btc_usd / btc_price
    gold_tons = gold_usd / xau_price / 32150.7466

    profit_match = re.search(
        r"net operating profit.*?reached approximately\s*\$([0-9.]+)\s*billion",
        article_text,
        re.I,
    )
    excess_match = re.search(
        r"exceeds\s+the\s+value\s+of\s+the\s+liabilities.*?by\s+US\$\s*([\d,]+)",
        pdf_text,
        re.I | re.S,
    )
    added_gold_match = re.search(r"added\s+([0-9.]+)\s+tons? of physical gold", article_text, re.I)

    return {
        "quarter": quarter,
        "as_of": as_of,
        "btc_usd": btc_usd,
        "btc_count": btc_count,
        "btc_valuation_price_usd": btc_price,
        "btc_count_method": "btc_usd / attestation BTC valuation price",
        "gold_usd": gold_usd,
        "gold_tons": gold_tons,
        "gold_added_tons": float(added_gold_match.group(1)) if added_gold_match else None,
        "xau_valuation_price_usd_per_oz": xau_price,
        "treasury_bills_usd": treasury_bills,
        "overnight_repo_usd": overnight_repo,
        "term_repo_usd": term_repo,
        "treasury_and_repo_usd": treasury_bills + overnight_repo + term_repo,
        "net_operating_profit_usd": float(profit_match.group(1)) * 1e9 if profit_match else None,
        "excess_reserves_usd": int(excess_match.group(1).replace(",", "")) if excess_match else None,
        "total_assets_usd": total_assets,
        "source_article": article_url,
        "source_pdf": pdf_url,
    }


def persist_quarterly(items: list[dict]) -> list[dict]:
    """Persist official quarterly points in a source-isolated table.

    The legacy ``tether_snapshots`` table is intentionally left untouched for
    forensic history, but it is no longer read by the report.
    """
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tether_quarterly_snapshots (
            quarter_end TEXT PRIMARY KEY,
            quarter TEXT,
            btc_holdings REAL,
            btc_usd REAL,
            btc_valuation_price_usd REAL,
            btc_count_method TEXT,
            source_article TEXT,
            source_pdf TEXT
        )
        """
    )
    for item in items:
        conn.execute(
            "INSERT OR REPLACE INTO tether_quarterly_snapshots "
            "(quarter_end, quarter, btc_holdings, btc_usd, btc_valuation_price_usd, "
            "btc_count_method, source_article, source_pdf) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item["as_of"], item["quarter"], item["btc_count"], item["btc_usd"],
                item["btc_valuation_price_usd"], item["btc_count_method"],
                item["source_article"], item["source_pdf"],
            ),
        )
    conn.commit()
    cur = conn.execute(
        "SELECT quarter_end, quarter, btc_holdings, btc_usd, btc_valuation_price_usd, "
        "btc_count_method, source_article, source_pdf "
        "FROM tether_quarterly_snapshots ORDER BY quarter_end"
    )
    history = [
        {
            "as_of": d, "quarter": q, "btc_count": b, "btc_usd": u,
            "btc_valuation_price_usd": p, "btc_count_method": method,
            "source_article": article, "source_pdf": pdf,
        }
        for d, q, b, u, p, method, article, pdf in cur.fetchall()
    ]
    conn.close()
    return history


def discover_quarterly_history(latest: dict) -> list[dict]:
    """Backfill new official quarters, then return the comparable series."""
    DB.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tether_quarterly_snapshots (
            quarter_end TEXT PRIMARY KEY, quarter TEXT, btc_holdings REAL,
            btc_usd REAL, btc_valuation_price_usd REAL, btc_count_method TEXT,
            source_article TEXT, source_pdf TEXT
        )
        """
    )
    known_articles = {
        row[0] for row in conn.execute(
            "SELECT source_article FROM tether_quarterly_snapshots WHERE source_article IS NOT NULL"
        )
    }
    conn.close()

    new_items = [latest]
    response = requests.get(
        URL_WP_POSTS,
        params={"search": "attestation", "per_page": 100},
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()
    for post in response.json():
        if (post.get("date") or "")[:10] < "2024-01-01":
            continue
        article_url = post.get("link")
        if not article_url or article_url in known_articles or article_url == latest["source_article"]:
            continue
        title = BeautifulSoup((post.get("title") or {}).get("rendered", ""), "lxml").get_text(" ", strip=True)
        title_lower = title.lower()
        if not any(marker in title_lower for marker in ("attestation", "profit", "performance")):
            continue
        article_html = (post.get("content") or {}).get("rendered", "")
        article_text = BeautifulSoup(article_html, "lxml").get_text(" ", strip=True)
        candidate_text = f"{title} {article_text}".lower()
        if "tether" not in candidate_text:
            continue
        if not any(marker in candidate_text for marker in ("attestation", "assurance")):
            continue
        if not any(marker in candidate_text for marker in ("q1", "q2", "q3", "q4", "quarter")):
            continue
        try:
            new_items.append(parse_quarterly_history_item(article_url, article_html))
        except Exception as exc:
            print(f"WARN quarterly history skip {article_url}: {exc}", file=sys.stderr)
    return persist_quarterly(new_items)


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    today = date.today().isoformat()

    try:
        bt_data = fetch_bitcointreasuries()
    except Exception as exc:
        bt_data = {"error": str(exc)}

    transparency = fetch_transparency_links()

    try:
        quarterly = discover_latest_quarter()
    except Exception as exc:
        output = {
            "fetched_at": today,
            "error": f"latest quarterly attestation fetch failed: {exc}",
            "current_holdings": bt_data,
            "transparency_page": transparency,
        }
        OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"FAIL: {output['error']}", file=sys.stderr)
        return 1

    try:
        quarterly_history = discover_quarterly_history(quarterly)
    except Exception as exc:
        print(f"WARN quarterly history backfill failed: {exc}", file=sys.stderr)
        quarterly_history = persist_quarterly([quarterly])

    output = {
        "fetched_at": today,
        "current_holdings": bt_data,
        "quarterly_reserves": quarterly,
        "transparency_page": transparency,
        "quarterly_history": quarterly_history,
        # Compatibility alias: unlike the old mixed daily table, this is now
        # strictly the official quarterly series.
        "history": quarterly_history,
        "context": (
            "Tether quarterly BTC and gold reserve values are parsed from official attestation PDFs. "
            "The BTC-unit history includes only quarters whose PDF discloses both BTC USD value and valuation price."
        ),
    }
    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(
        f"OK: wrote {OUT} ({quarterly['quarter']}, BTC={quarterly['btc_count']:,.0f}, "
        f"value=${quarterly['btc_usd']/1e9:.3f}B; quarterly_history={len(quarterly_history)})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
