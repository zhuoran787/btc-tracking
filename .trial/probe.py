"""Isolated cloud collection experiment. No report publication or model claims."""
import concurrent.futures
import hashlib
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode
import requests
from bs4 import BeautifulSoup

OUT = Path('.trial-results')
OUT.mkdir(exist_ok=True)
START = datetime.now(timezone.utc)

def fetch(name, url):
    started = time.monotonic()
    try:
        with requests.Session() as session:
            response = session.get(url, timeout=(12, 35), headers={'User-Agent': 'Mozilla/5.0'})
            response.raise_for_status()
        raw = response.content
        (OUT / (name + '.body')).write_bytes(raw)
        soup = BeautifulSoup(raw, 'html.parser')
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        for el in soup(['script', 'style', 'nav', 'footer', 'header']):
            el.decompose()
        text = soup.get_text('\n', strip=True)
        (OUT / (name + '.txt')).write_text(text)
        return {'name': name, 'url': url, 'final_url': response.url, 'status': response.status_code,
                'bytes': len(raw), 'title': title, 'sha256': hashlib.sha256(raw).hexdigest(),
                'seconds': round(time.monotonic() - started, 2),
                'note': 'HTTP success is not verification of article contents or date'}
    except Exception as exc:
        return {'name': name, 'url': url, 'error': str(exc), 'seconds': round(time.monotonic() - started, 2)}

def rss(spec):
    url = 'https://news.google.com/rss/search?' + urlencode({
        'q': spec['query'] + ' when:' + str(spec['days']) + 'd',
        'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})
    result = fetch('rss-' + str(spec['id']), url)
    result.update(spec)
    result['items'] = []
    if 'error' not in result:
        try:
            root = ET.fromstring((OUT / ('rss-' + str(spec['id']) + '.body')).read_bytes())
            for item in root.findall('.//item'):
                date = item.findtext('pubDate')
                try:
                    observed = parsedate_to_datetime(date)
                    fresh = START - timedelta(days=spec['days']) <= observed <= START + timedelta(minutes=5)
                except (ValueError, TypeError):
                    fresh = False
                result['items'].append({'title': item.findtext('title'), 'pubDate': date,
                                        'url': item.findtext('link'), 'source': item.findtext('source'),
                                        'in_window': fresh})
            result['fresh_count'] = sum(i['in_window'] for i in result['items'])
        except Exception as exc:
            result['error'] = 'RSS parse failed: ' + str(exc)
    result['needs_web_search_fallback'] = not result.get('fresh_count')
    return result

fixed = [
    ('vaneck-index', 'https://www.vaneck.com/us/en/insights/thought-leaders/matthew-sigel/'),
    ('galaxy-index', 'https://www.galaxy.com/insights/research/'),
    ('trendresearch-index', 'https://trendresearch.medium.com/'),
]
# Fixed channels must be checked before the independent RSS requests.
fixed_results = [fetch(*target) for target in fixed]
specs = json.loads(Path('.trial/queries.json').read_text())['queries']
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    rss_results = list(pool.map(rss, specs))
article_targets = [
    ('vaneck-article', 'https://www.vaneck.com/cl/en/news-and-insights/blogs/digital-assets/matthew-sigel-vaneck-mid-september-2026-bitcoin-chaincheck/'),
    ('bitwise-article', 'https://experts.bitwiseinvestments.com/cio-memos/the-clarity-acts-failure-is-a-speed-bump-not-a-roadblock'),
    ('sigel-x', 'https://x.com/matthew_sigel'),
]
articles = [fetch(*target) for target in article_targets]
result = {'environment': 'GitHub Actions' if os.getenv('GITHUB_ACTIONS') else 'local',
          'started_at': START.isoformat(), 'finished_at': datetime.now(timezone.utc).isoformat(),
          'run_id': os.getenv('GITHUB_RUN_ID'), 'fixed_channels': fixed_results, 'rss': rss_results,
          'article_probes': articles,
          'counts': {'requests': len(specs), 'rss_fetch_ok': sum('error' not in r for r in rss_results),
                     'queries_with_fresh_results': sum(bool(r.get('fresh_count')) for r in rss_results),
                     'needs_web_search_fallback': sum(r['needs_web_search_fallback'] for r in rss_results)},
          'ai_research': 'not_run: authentication not configured',
          'publication': 'disabled', 'complete_sop_passed': False}
(OUT / 'collection.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result['counts']))
print('Collection experiment complete; this does not certify the full SOP.')
