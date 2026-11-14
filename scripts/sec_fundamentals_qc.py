#!/usr/bin/env python3
"""QC report for SEC fundamental facts and normalized metrics."""
from __future__ import annotations
import json, sys
from pathlib import Path
from sqlalchemy import text
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from db_builder.config import local_engine

with local_engine().connect() as c:
    as_of=c.execute(text("select max(date) from public.us_equities")).scalar()
    out={}
    out['as_of']=str(as_of)
    out['coverage']=dict(c.execute(text("select status,count(*) from fundamentals.sec_coverage group by status")).fetchall())
    out['distinct_ok']=c.execute(text("select count(distinct ticker) from fundamentals.sec_coverage where status='ok'")).scalar()
    out['facts']=c.execute(text("select count(*) from fundamentals.sec_facts")).scalar()
    out['metrics']=c.execute(text("select count(*) from fundamentals.sec_metrics")).scalar()
    out['duplicate_facts']=c.execute(text("""select count(*) from (select cik,tag,unit,coalesce(start_date,date '0001-01-01'),end_date,accession,count(*) from fundamentals.sec_facts group by 1,2,3,4,5,6 having count(*)>1) x""")).scalar()
    out['period_types']=dict(c.execute(text("select period_type,count(*) from fundamentals.sec_facts group by period_type")).fetchall())
    out['units']=dict(c.execute(text("select unit,count(*) from fundamentals.sec_facts group by unit order by count(*) desc limit 12")).fetchall())
    out['metric_availability']=dict(c.execute(text("select metric,count(*) from fundamentals.sec_metrics group by metric order by metric")).fetchall())
    out['accepted_basis']=dict(c.execute(text("select availability_basis,count(*) from fundamentals.sec_facts group by availability_basis")).fetchall())
    out['pit_violations']=c.execute(text("select count(*) from fundamentals.sec_facts where available_at>:d"),{'d':as_of}).scalar()
    out['restatements']=c.execute(text("select count(*) from fundamentals.sec_facts where is_restatement")).scalar()
    out['share_basis_unverified']=c.execute(text("select count(*) from fundamentals.sec_metrics where share_basis_status='unverified'")).scalar()
    out['outliers']={
      'revenue_negative':c.execute(text("select count(*) from fundamentals.sec_metrics where metric='revenue' and value<0")).scalar(),
      'margin_gt_100':c.execute(text("select count(*) from fundamentals.sec_metrics where metric in ('operating_margin','net_margin') and abs(value)>100")).scalar(),
      'eps_gt_1000':c.execute(text("select count(*) from fundamentals.sec_metrics where metric='diluted_eps' and abs(value)>1000")).scalar(),
    }
    out['storage_bytes']=c.execute(text("select pg_total_relation_size('fundamentals.sec_facts')+pg_total_relation_size('fundamentals.sec_metrics')")).scalar()
    out['tags']=dict(c.execute(text("select tag,count(*) from fundamentals.sec_facts group by tag order by count(*) desc limit 20")).fetchall())
    out['sectors']=dict(c.execute(text("""
      select coalesce(s.sector,(select x.sector from public.security_classification x
        where x.ticker=m.ticker order by x.is_active desc limit 1)) as sector,
        count(distinct m.ticker)
      from fundamentals.sec_metrics m left join public.security_classification s
        on s.ticker=m.ticker and s.is_active=true
      group by 1 order by 1"""
    )).fetchall())
    # One latest annual example per GICS sector, preferring names with revenue and margin.
    examples=[]
    for sector in [x[0] for x in c.execute(text("select distinct sector from public.security_classification where sector is not null order by sector")).fetchall()]:
        row=c.execute(text("""
          select m.ticker,
            max(case when m.metric='revenue' then m.value end) as revenue,
            max(case when m.metric='operating_margin' then m.value end) as operating_margin,
            max(case when m.metric='net_margin' then m.value end) as net_margin,
            max(case when m.metric='diluted_eps' then m.value end) as diluted_eps,
            max(case when m.metric='revenue_growth_yoy' then m.value end) as revenue_growth,
            max(m.period_end) as period_end
          from fundamentals.sec_metrics m
          join public.security_classification s on s.ticker=m.ticker and s.is_active=true
          where s.sector=:s and m.period_type='Y'
          group by m.ticker having max(case when m.metric='revenue' then m.value end) is not null
          order by m.ticker limit 1
        """),{'s':sector}).fetchone()
        if row:
            examples.append({'sector':sector,'ticker':row[0],'revenue':float(row[1]) if row[1] is not None else None,
                             'operating_margin':float(row[2]) if row[2] is not None else None,
                             'net_margin':float(row[3]) if row[3] is not None else None,
                             'diluted_eps':float(row[4]) if row[4] is not None else None,
                             'revenue_growth':float(row[5]) if row[5] is not None else None,
                             'period_end':str(row[6])})
    out['sector_examples']=examples
print(json.dumps(out,indent=2,default=str))
