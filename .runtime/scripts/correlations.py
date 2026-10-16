"""Calendar-month, synchronized-return correlations. Shared by fetch/render/audit.

Policy lives in correlation_policy.json. No source substitutions or price filling.
"""
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import exchange_calendars as xc
import numpy as np
import pandas as pd
import requests

POLICY_PATH = Path(__file__).with_name('correlation_policy.json')
POLICY = json.loads(POLICY_PATH.read_text())


def policy_hash():
    return hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()


def calendar():
    return xc.get_calendar(POLICY['reference_calendar'])


def latest_completed_session(now=None):
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise ValueError('Correlation reference time requires a timezone')
    dates = calendar().schedule.loc[(now-pd.Timedelta(days=20)).strftime('%Y-%m-%d'):now.strftime('%Y-%m-%d')]
    final = dates['close'] + pd.Timedelta(minutes=POLICY['close_finalization_minutes'])
    return dates.index[final <= now][-1].strftime('%Y-%m-%d')


def _positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('Invalid correlation price')
    return value


def _put(target, key, value):
    if key in target and target[key] != value:
        raise ValueError(f'Conflicting duplicate correlation observation: {key}')
    target[key] = value


def _midnights(history):
    result = {}
    for row in history:
        ts = pd.Timestamp(row['ts'], unit='ms', tz='UTC')
        if ts != ts.normalize():
            continue  # Exclude current intraday tail; it must not overwrite midnight.
        _put(result, ts.strftime('%Y-%m-%d'), _positive(row['price']))
    return result


def _sox_dates(history):
    result = {}
    for row in history:
        # Yahoo daily timestamp labels a NY session, not an intraday price time.
        ts = pd.Timestamp(row['ts'], unit='ms', tz='UTC').tz_convert('America/New_York')
        if ts != ts.normalize():
            raise ValueError('Unexpected SOX daily session timestamp')
        _put(result, ts.strftime('%Y-%m-%d'), _positive(row['price']))
    return result


def fetch_btc_session_closes(as_of, cache_path, now=None):
    """Coinbase last trade before scheduled US close, with persistent daily cache."""
    cache_path = Path(cache_path)
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    start = pd.Timestamp(as_of) - pd.DateOffset(years=POLICY['chart_years'])
    schedule = calendar().schedule.loc[start:as_of]
    existing = {}
    if cache_path.exists():
        old = json.loads(cache_path.read_text())
        if old.get('config_hash') == policy_hash():
            for row in old.get('history', []):
                _put(existing, row['date'], row)
    refresh = set(schedule.index[-5:].strftime('%Y-%m-%d'))
    required = {}
    for day, session in schedule.iterrows():
        date = day.strftime('%Y-%m-%d')
        close_ts = int(session['close'].timestamp()*1000)
        prior = existing.get(date)
        if not prior or prior['ts'] != close_ts or date in refresh:
            required[date] = session['close']
    candles = {}
    if required:
        first = min(required.values()) - pd.Timedelta(hours=1)
        stop = max(required.values())
        cursor = first
        while cursor < stop:
            end = min(cursor + pd.Timedelta(days=12), stop)
            params = {'start':cursor.isoformat(), 'end':end.isoformat(), 'granularity':3600}
            response = requests.get('https://api.exchange.coinbase.com/products/BTC-USD/candles',
                                    params=params, timeout=30)
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError('Coinbase did not return candle rows')
            for row in rows:
                if len(row) != 6:
                    raise ValueError('Coinbase candle schema changed')
                ts = int(row[0])
                if cursor.timestamp() <= ts < end.timestamp():
                    _put(candles, ts, row)
            cursor = end
        for date, close in required.items():
            bucket = int(close.timestamp()) - 3600
            row = candles.get(bucket)
            if not row or row[5] <= 0 or close > now:
                raise ValueError(f'Missing/completing BTC closing candle: {date}')
            existing[date] = {'date':date, 'ts':int(close.timestamp()*1000),
                              'price':_positive(row[4]), 'candle_start_ts':bucket*1000,
                              'closing_hour_volume':row[5]}
    history = [existing[d] for d in schedule.index.strftime('%Y-%m-%d')]
    payload = {'config_hash':policy_hash(), 'source':POLICY['pairs']['btc_sox']['btc_source'],
               'source_url':'https://api.exchange.coinbase.com/products/BTC-USD/candles',
               'as_of_date':as_of, 'history':history}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp = cache_path.with_suffix('.tmp')
    temp.write_text(json.dumps(payload, indent=2))
    temp.replace(cache_path)
    return payload


def _pair_rows(api, as_of):
    start = pd.Timestamp(as_of) - pd.DateOffset(years=POLICY['chart_years'])
    utc_btc = _midnights(api['btc_price']['history'])
    gold = _midnights(api['gold_price']['history'])
    if api['gold_price'].get('source') != POLICY['pairs']['btc_gold']['comparison_source']:
        raise ValueError('Gold source must be the explicitly selected XAUT series')
    us = api['btc_us_close']
    if us.get('config_hash') != policy_hash() or us.get('source') != POLICY['pairs']['btc_sox']['btc_source']:
        raise ValueError('BTC close data does not match correlation policy')
    us_btc = {}
    for row in us['history']:
        _put(us_btc, row['date'], row)
    sox = _sox_dates(api['sox_price']['history'])
    pairs = {key:[] for key in POLICY['pairs']}
    # Chart may have fewer than one year of points from upstream, but all required
    # correlation-window dates are checked below; never fill a price or skip a gap.
    for day in pd.date_range(start, as_of):
        date = day.strftime('%Y-%m-%d')
        if date in utc_btc and date in gold:
            pairs['btc_gold'].append({'date':date, 'ts':int(day.tz_localize('UTC').timestamp()*1000),
                                      'btc':utc_btc[date], 'comparison':gold[date]})
    for day, session in calendar().schedule.loc[start:as_of].iterrows():
        date = day.strftime('%Y-%m-%d')
        if date in us_btc and date in sox:
            row = us_btc[date]
            ts = int(session['close'].timestamp()*1000)
            if row['ts'] != ts or row.get('candle_start_ts') != ts-3600000 or row.get('closing_hour_volume',0) <= 0:
                raise ValueError(f'BTC/SOX close-time mismatch: {date}')
            pairs['btc_sox'].append({'date':date, 'ts':ts, 'btc':_positive(row['price']),
                                    'comparison':sox[date]})
    return pairs


def compute_correlations(api, as_of=None, now=None):
    as_of = as_of or latest_completed_session(now)
    if as_of > latest_completed_session(now):
        raise ValueError('Correlation includes unfinished/future US session')
    if not calendar().is_session(as_of):
        raise ValueError('Correlation reference date is not a completed US session')
    pairs = _pair_rows(api, as_of)
    values, metadata = {}, {}
    for name, definition in POLICY['pairs'].items():
        for months in POLICY['horizons_months']:
            key = f'{name}_{months}m'
            cutoff = (pd.Timestamp(as_of)-pd.DateOffset(months=months)).strftime('%Y-%m-%d')
            if definition['calendar'] == 'UTC_daily':
                expected = pd.date_range(cutoff,as_of).strftime('%Y-%m-%d').tolist()
            else:
                expected = calendar().sessions_in_range(cutoff,as_of).strftime('%Y-%m-%d').tolist()
            selected = [r for r in pairs[name] if cutoff <= r['date'] <= as_of]
            if [r['date'] for r in selected] != expected:
                raise ValueError(f'{key}: missing dates or incomplete calendar window')
            prices = np.asarray([[r['btc'],r['comparison']] for r in selected])
            returns = prices[1:]/prices[:-1]-1
            if len(returns)<2 or not np.isfinite(returns).all() or np.any(np.std(returns,axis=0)==0):
                raise ValueError(f'{key}: invalid or zero-variance returns')
            value = float(np.corrcoef(returns[:,0],returns[:,1])[0,1])
            # Independent arithmetic catches a formula/regression mismatch.
            x,y = returns[:,0].tolist(),returns[:,1].tolist()
            xm,ym = sum(x)/len(x),sum(y)/len(y)
            manual = sum((a-xm)*(b-ym) for a,b in zip(x,y))/math.sqrt(
                sum((a-xm)**2 for a in x)*sum((b-ym)**2 for b in y))
            if abs(value-manual)>1e-12:
                raise ValueError(f'{key}: independent Pearson check failed')
            values[key] = value
            metadata[key] = {'config_hash':policy_hash(), 'methodology_id':POLICY['methodology_id'],
                'months':months, 'calendar_cutoff':cutoff, 'start_date':selected[0]['date'],
                'end_date':as_of, 'start_timestamp':selected[0]['ts'], 'end_timestamp':selected[-1]['ts'],
                'observations':len(returns), 'price_points':len(selected), 'missing_observations':0,
                'clock':definition['clock'], 'calendar':definition['calendar'],
                'btc_source':definition['btc_source'], 'comparison_source':definition['comparison_source'],
                'method':'Pearson correlation of simple returns between synchronized price endpoints'}
    return {'correlations':values, 'correlations_meta':metadata, 'correlation_pairs':pairs,
            'correlations_as_of':as_of, 'correlation_config_hash':policy_hash()}


def validate_correlations(api, now=None):
    if api.get('correlation_config_hash') != policy_hash():
        raise ValueError('Correlation policy changed or missing; recompute before rendering/publishing')
    actual = compute_correlations(api,as_of=api['correlations_as_of'],now=now)
    for field in ('correlations_meta','correlation_pairs'):
        if actual[field] != api.get(field):
            raise ValueError(f'Correlation {field} disagrees with its input prices/policy')
    for key,value in actual['correlations'].items():
        stored = api.get('correlations',{}).get(key)
        if stored is None or not math.isfinite(stored) or abs(value-stored)>1e-12:
            raise ValueError(f'{key} differs from independently checked recalculation')
    return actual['correlations_meta']


if __name__ == '__main__':
    import sys
    data = json.loads(Path(sys.argv[1]).read_text())
    print(json.dumps(validate_correlations(data),ensure_ascii=False,indent=2))
