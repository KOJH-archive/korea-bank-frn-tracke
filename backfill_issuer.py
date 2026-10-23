"""마스터의 실제 발행일에 해당하는 공개 보고서를 좁혀 과거 조건을 대조한다."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
import json
from pathlib import Path
import sqlite3

from db import get_connection
from frn_analysis import load_bank_frn
from issuance_terms import init_issue_schema,save_issue_observation
from kis_issuance import discover_reports,extract_report,request

ROOT=Path(__file__).resolve().parent


def prepare(report):
    folder=ROOT/'data'/'kis_reports'
    path=folder/(report['id']+'.pdf')
    try:
        if not path.exists():
            blob=request(report['url'])
            if not blob.startswith(b'%PDF'):
                raise ValueError('PDF 응답이 아닙니다.')
            path.write_bytes(blob)
        rows,skipped,periods=extract_report(path,report['url'])
        return dict(report,rows=rows,skipped=skipped,periods=periods)
    except Exception as exc:
        return dict(report,error=f'{type(exc).__name__}: {exc}',rows=[],skipped=[],periods=[])


def backfill(issuer,start,end):
    folder=ROOT/'data'/'kis_reports'
    folder.mkdir(parents=True,exist_ok=True)
    manifest=folder/f'manifest_{start}_{end}.json'
    reports=json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else discover_reports(start,end)
    if not manifest.exists():
        manifest.write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
    conn=get_connection()
    init_issue_schema(conn)
    frame=load_bank_frn(conn)
    targets=frame[(frame.issuer==issuer)&(frame.issue_date>=start)&(frame.issue_date<=end)&frame.maturity_bucket.astype(str).isin(['1년','1.5년'])]
    issue_dates=[datetime.strptime(x,'%Y%m%d') for x in targets.issue_date.unique()]
    selected=[r for r in reports if any(d<=datetime.strptime(r['report_date'],'%Y%m%d')<=d+timedelta(days=10) for d in issue_dates)]
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    backup=ROOT/f'bond_before_backfill_{stamp}.db'
    with sqlite3.connect(backup) as b:
        conn.backup(b)
    totals=Counter()
    logs=[]
    print(json.dumps(dict(target_candidates=len(targets),reports=len(selected)),ensure_ascii=True),flush=True)
    # 독립 PDF 요청·추출만 병렬 처리하며 SQLite 저장은 주 스레드에서 순차 처리한다.
    with ThreadPoolExecutor(max_workers=3) as pool:
        for report in pool.map(prepare,selected):
            statuses=Counter()
            if report.get('periods'):
                with conn:
                    for row in report['rows']:
                        if start<=row['issue_date']<=end:
                            statuses[save_issue_observation(conn,row)]+=1
            else:
                statuses['NO_FRN_APPENDIX_OR_ERROR']+=1
            totals.update(statuses)
            logs.append(dict(report,statuses=dict(statuses)))
            print(json.dumps(dict(report=report['report_date'],statuses=dict(statuses),rows=len(report['rows'])),ensure_ascii=True),flush=True)
    conn.close()
    result=dict(issuer=issuer,start=start,end=end,candidate_isins=targets.isin_code.tolist(),
                totals=dict(totals),reports=logs,backup=str(backup),completed_at=datetime.now().isoformat(timespec='seconds'))
    (ROOT/'data'/'shinhan_backfill_report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--issuer',required=True)
    parser.add_argument('--start',required=True)
    parser.add_argument('--end',required=True)
    args=parser.parse_args()
    print(json.dumps(backfill(args.issuer,args.start,args.end)['totals'],ensure_ascii=True))
