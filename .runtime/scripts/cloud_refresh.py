"""Daily data-only refresh. Runs in an allowlisted, standalone Pages repository."""
import concurrent.futures
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
CONFIG = ROOT / 'publication/config.json'


def read(p):
    return json.loads(Path(p).read_text())


def digest(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def nested(d, path):
    for k in path.split('.'):
        d = d[int(k)] if isinstance(d, list) else d[k]
    return d


def validate(name, d, rules):
    if d.get('error') or d.get('errors') or d.get('fallback_needed'):
        raise ValueError('Source reported an error or fallback')
    for path, minimum in rules.get('lengths', {}).items():
        if len(nested(d, path)) < minimum:
            raise ValueError(f'Insufficient history: {path}')
    for path in rules.get('required', []):
        v = nested(d, path)
        if v is None or v == '' or v == {} or v == []:
            raise ValueError(f'Missing: {path}')
    for path in rules.get('positive', []):
        if not float(nested(d, path)) > 0:
            raise ValueError(f'Not positive: {path}')
    for path, expected in rules.get('equals', {}).items():
        if nested(d, path) != expected:
            raise ValueError(f'Unexpected methodology/sample: {path}')
    for path in rules.get('no_error', []):
        block = nested(d, path)
        if block.get('error') or block.get('skipped') or block.get('stale'):
            raise ValueError(f'Unavailable subsource: {path}')
    for path, days in rules.get('max_age_days', {}).items():
        value = nested(d, path)
        if isinstance(value, (int, float)):
            date = datetime.fromtimestamp(value / (1000 if value > 1e11 else 1), timezone.utc)
        else:
            date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - date).total_seconds() / 86400
        if age < -1 or age > days:
            raise ValueError(f'Stale or future observation: {path}')


def source_date(d):
    if d.get('btc_price', {}).get('history'):
        ts = d['btc_price']['history'][-1]['ts']
        return datetime.fromtimestamp(ts / (1000 if ts > 1e11 else 1), timezone.utc).isoformat()
    if d.get('timestamp'):
        return datetime.fromtimestamp(d['timestamp'], timezone.utc).date().isoformat()
    if d.get('dates'):
        return str(d['dates'][-1])
    for path in ['current_date', 'summary.latest_date', 'latest.date', 'last_updated',
                 'quarterly_reserves.as_of', 'fetched_at', 'generated_at']:
        try:
            value = nested(d, path)
            if value:
                return str(value)
        except (KeyError, TypeError):
            pass
    return '未披露'


def fetch_one(spec, config):
    # Each scraper gets an isolated candidate directory, so a failure cannot
    # overwrite the last successful JSON or append bad rows to history databases.
    with tempfile.TemporaryDirectory(prefix='btc-source-') as temp:
        candidate = Path(temp)
        shutil.copytree(ROOT / 'scripts', candidate / 'scripts')
        shutil.copytree(ROOT / 'data', candidate / 'data')
        (candidate / 'cache').mkdir()
        key = os.environ.get('COINALYZE_API_KEY')
        if key and spec['script'] == 'fetch_derivatives.py':
            (candidate / '.env').write_text('COINALYZE_API_KEY=' + key + '\n')
        try:
            p = subprocess.run([sys.executable, str(candidate / 'scripts' / spec['script'])],
                               cwd=candidate, capture_output=True, text=True,
                               timeout=config['fetch_timeout_seconds'])
            log = p.stdout + '\n' + p.stderr
            if key:
                log = log.replace(key, '[REDACTED]')
            (REPO / '.receipts' / (spec['script'] + '.log')).write_text(log)
            output = candidate / 'data' / spec['output']
            if p.returncode:
                raise ValueError(f'Fetcher exit {p.returncode}')
            if not output.exists():
                raise ValueError('No output')
            d = read(output)
            validate(spec['output'], d, spec['validation'])
            if output.read_bytes() == (ROOT / 'data' / spec['output']).read_bytes():
                raise ValueError('Fetcher left output unchanged; not verified this run')
            for name in [spec['output']] + spec.get('sidecars', []):
                shutil.copy2(candidate / 'data' / name, ROOT / 'data' / name)
            return {'source': spec['output'], 'status': 'updated', 'source_date': source_date(d),
                    'sha256': digest(ROOT / 'data' / spec['output'])}
        except Exception as exc:
            previous = read(ROOT / 'data' / spec['output'])
            return {'source': spec['output'], 'status': 'unavailable' if previous.get('error') else 'retained',
                    'source_date': '无有效快照' if previous.get('error') else source_date(previous),
                    'reason': str(exc), 'sha256': digest(ROOT / 'data' / spec['output'])}


def load_extra(g):
    # No writes to user/editorial caches; no fresh timestamp is assigned to them.
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO('{}')
        with contextlib.redirect_stdout(io.StringIO()):
            extra = g.load_extra_input()
    finally:
        sys.stdin = old_stdin
    cached = read(ROOT / 'data/websearch_cache.json')
    for key in ['factor_9_reserve', 'factor_10_institution', 'factor_11_clarity']:
        extra[key] = cached.get(key, {}).get('text')
    return extra


def make_page(config, results, bootstrap=False):
    import generate_report as g
    from bs4 import BeautifulSoup
    editorial = read(ROOT / 'publication/editorial.json')
    extra = load_extra(g)
    ctx = g.build_context(g.load_data(), extra)
    # These are dated source/news snapshots, not the manually rewritten September
    # report summaries. Data-derived summaries continue through the usual helpers.
    ctx.update(editorial['context'])
    for field in ['websearch_freshness', 'user_inputs_freshness']:
        for value in (ctx.get(field) or {}).values():
            if isinstance(value, dict):
                value['status'] = 'cached'
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    ctx['report_date'] = now
    frozen = ['opinions_input.json', 'websearch_cache.json', 'user_inputs_cache.json']
    public = {'mode': 'data_only', 'built_at': now, 'bootstrap': bootstrap,
              'config_hash': digest(CONFIG), 'editorial_as_of': editorial['as_of'],
              'schedule': config['schedule'], 'sources': results,
              'preserved_inputs': {p: digest(ROOT / 'data' / p) for p in frozen}}
    html = g.render(ctx)
    soup = BeautifulSoup(html, 'html.parser')
    labels = {s['output']: s['label'] for s in config['fetchers']}
    table = ''.join('<tr><td>'+escape(labels[x['source']])+'</td><td>'+
                    ('本次抓取通过' if x['status']=='updated' else '沿用旧数据 / 本次未更新')+
                    '</td><td>'+escape(x['source_date'])+'</td></tr>' for x in results)
    banner = '<div style="padding:18px;margin:16px;border:2px solid #b58a36;background:#fff7dc;color:#342e21">'
    banner += '<strong>每日云端数据更新 · '+escape(config['schedule']['label'])+'</strong><br>'
    display_time = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M 北京时间')
    banner += ('初始化展示；尚未完成一次云端数据刷新。' if bootstrap else '本次数据检查：'+display_time+'。')
    banner += ' 新闻和多空观点沿用 '+escape(editorial['as_of'])+' 的已核材料，各条保留原日期；五项个人判断沿用原文及确认日期。'
    banner += ' 数据相关总结按本次可用数据计算；旧观点不代表当前判断。<br>各源状态和原始截止日期如下。'
    banner += '<details><summary>查看数据源更新状态</summary><table><tr><th>数据源</th><th>状态</th><th>原始日期</th></tr>'+table+'</table></details></div>'
    soup.body.insert(0, BeautifulSoup(banner, 'html.parser'))
    # Historical screenshots must never inherit the page generation timestamp.
    html = str(soup).replace('抓取于 '+now, '历史截图沿用，非本次抓取')
    html = html.replace('人本周有更新', '人在上次完整采集中有更新')
    html = html.replace('>具体观点跟踪<', '>具体观点跟踪（沿用 '+escape(editorial['as_of'])+' 已核观点）<')
    if '/Users/' in html or 'file:///' in html:
        raise ValueError('Local path found in public HTML')
    if 'VanEck' not in html:
        raise ValueError('VanEck block missing')
    if len(BeautifulSoup(html, 'html.parser').select('canvas')) < 10:
        raise ValueError('Chart layout incomplete')
    site = REPO / '.site'
    site.mkdir(exist_ok=True)
    (site / 'index.html').write_text(html)
    (site / 'publication.json').write_text(json.dumps(public, ensure_ascii=False, indent=2)+'\n')
    shutil.copy2(site / 'index.html', REPO / 'index.html')
    shutil.copy2(site / 'publication.json', REPO / 'publication.json')
    return public


def main():
    config = read(CONFIG)
    bootstrap = '--bootstrap' in sys.argv
    audit_dir = REPO / '.receipts'
    audit_dir.mkdir(exist_ok=True)
    frozen = {p: digest(ROOT / 'data' / p) for p in
              ['opinions_input.json','websearch_cache.json','user_inputs_cache.json']}
    if bootstrap:
        results = [{'source': s['output'], 'status': 'retained',
                    'source_date': source_date(read(ROOT/'data'/s['output']))} for s in config['fetchers']]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config['workers']) as pool:
            results = list(pool.map(lambda spec: fetch_one(spec, config), config['fetchers']))
        (audit_dir / 'fetch-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
        required = {s['output'] for s in config['fetchers'] if s.get('required_for_publish')}
        if any(x['status']!='updated' for x in results if x['source'] in required):
            raise SystemExit('Core price/data refresh failed; keep previous published page')
    assert all(digest(ROOT/'data'/p)==h for p,h in frozen.items()), 'Editorial cache changed'
    result = make_page(config, results, bootstrap)
    (audit_dir/'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'updated':sum(x['status']=='updated' for x in results),
                      'retained':sum(x['status']=='retained' for x in results),
                      'bootstrap':bootstrap, 'url':config['url']}))


if __name__ == '__main__':
    main()
