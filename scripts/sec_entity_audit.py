#!/usr/bin/env python3
import sys, json, requests
sys.path.insert(0,r'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\src')
from db_builder.config import local_engine
from sqlalchemy import text
UA='AEGIS research contact@example.com'
sec=requests.get('https://www.sec.gov/files/company_tickers.json',headers={'User-Agent':UA},timeout=30).json()
by_t={str(v.get('ticker','')).upper():v for v in sec.values()}
with local_engine().connect() as c:
 rows=c.execute(text("select ticker,cik,status from fundamentals.sec_coverage order by ticker")).fetchall()
 out=[]
 for t,cik,status in rows:
  t=str(t).upper(); alt=t.replace('.','-'); s=by_t.get(alt) or by_t.get(t)
  if not s:
   out.append({'ticker':t,'db_cik':cik,'sec_cik':None,'status':status,'reason':'ticker not in SEC company_tickers'})
  elif str(s.get('cik_str','')).zfill(10)!=str(cik).zfill(10):
   out.append({'ticker':t,'db_cik':cik,'sec_cik':str(s['cik_str']).zfill(10),
               'sec_title':s.get('title'),'status':status,'reason':'CIK mismatch'})
 print(json.dumps({'audited':len(rows),'mismatches':out},indent=2))
