"""Read-only availability and freshness probes; does not change report inputs."""
from datetime import datetime, timezone
from pathlib import Path
import json, math, urllib.request

URLS = {
 'corecharts': 'https://corecharts.com/api/v1/metrics/series-daily?metric_key=mvrv_z_std&include_kpi=0&resolution=1d',
 'brk': 'https://bitview.space/api/series/bulk?series=date,market_cap,realized_cap&index=day1&start=0',
}

def get(url):
 req=urllib.request.Request(url,headers={'User-Agent':'BTC-Tracking-Source-Probe/1.0','Accept':'application/json'})
 with urllib.request.urlopen(req,timeout=45) as response:
  return json.load(response)

def main():
 now=datetime.now(timezone.utc);today=now.date().isoformat();result={'checked_at':now.isoformat(),'sources':{}}
 for source,url in URLS.items():
  try:
   d=get(url)
   if source=='corecharts':
    rows=d['series'];closed=[r for r in rows if r[0]<today]
    result['sources'][source]={'status':'ok','rows':len(rows),'latest':rows[-1],
      'latest_closed_day':closed[-1], 'metadata':d.get('meta'), 'url':url}
   else:
    assert len(d)==3 and len({(x['start'],x['end']) for x in d})==1
    dates,market,realized=[x['data'] for x in d]
    assert len(dates)==len(market)==len(realized)
    mean=m2=0.0;n=0;rows=[]
    for day,m,r in zip(dates,market,realized):
     if m is None:continue
     n+=1;delta=m-mean;mean+=delta/n;m2+=delta*(m-mean)
     sd=math.sqrt(max(0,m2/n))
     if r is not None and sd>0:rows.append([day,(m-r)/sd])
    closed=[r for r in rows if r[0]<today]
    result['sources'][source]={'status':'ok','raw_rows':len(dates),'latest_computed':rows[-1],
      'latest_closed_day_computed':closed[-1], 'source_stamps':[x['stamp'] for x in d],
      'method':'local calculation: (market_cap-realized_cap)/expanding population std(market_cap); includes non-null genesis history; not a native BRK Z-score', 'url':url}
  except Exception as exc:
   result['sources'][source]={'status':'failed','error':str(exc),'url':url}
 Path('probe-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(result,ensure_ascii=False,indent=2))
 if any(s['status']!='ok' for s in result['sources'].values()):raise SystemExit(1)
if __name__=='__main__':main()
