"""공개 KIS Weekly의 '변동금리채권 발행현황'에서 최초 발행조건을 적재.

python kis_issuance.py --start 20260101 --end 20261023
네트워크 재호출 없이 재검증: --cached-only
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import urllib.parse
import urllib.request

from config import DB_PATH
from db import get_connection
from issuance_terms import init_issue_schema, save_issue_observation, save_source_report

ROOT = Path(__file__).resolve().parent
LIST_URL = 'https://www.bond.co.kr/post/10106/10254'
REPORT_URL = 'https://www.bond.co.kr/post/dawn/'
PARSER_VERSION = 3


def request(url, data=None):
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers={
        'User-Agent': 'FRN-local-research/1.0'
    }), timeout=40) as response:
        return response.read()


def discover_reports(start, end):
    """사용자 화면의 공개 목록 요청. 존재하는 링크만 순서대로 수집."""
    found = {}
    for page in range(1, 101):
        payload = urllib.parse.urlencode({'page':page, 'kword':'', 'startDt':'', 'endDt':''}).encode()
        text = request(LIST_URL, payload).decode('utf-8')
        dates = []
        for item in re.findall(r'<li>(.*?)</li>', text, re.S):
            date = re.search(r'KISWeekly.*?\((\d{8})\)', item)
            link = re.search(r'href="/post/dawn/(\d+)"', item)
            if not date or not link:
                continue
            dates.append(date[1])
            if start <= date[1] <= end:
                found[link[1]] = {'id':link[1], 'report_date':date[1], 'url':REPORT_URL+link[1]}
        if not dates or min(dates) < start or "last='true'" in text:
            break
        time.sleep(0.2)
    return sorted(found.values(), key=lambda r:r['report_date'])


def parse_formula(raw):
    """3사/4사 민평을 분리. CD91·CP·KOFR 및 추측 가능한 문구는 섞지 않음."""
    compact = re.sub(r'\s+', '', raw).upper()
    match = re.fullmatch(r'민평([34])사AAA(?:등급)?CD(?:1개월|1M)([+-]\d+(?:\.\d+)?)(BP|%)', compact)
    if match:
        return f'CD1M_M{match[1]}_AAA', round(float(match[2])*(100 if match[3]=='%' else 1),6)
    match = re.fullmatch(r'(?:91일물CD|91물CD|CD91일물|CD91일)([+-]\d+(?:\.\d+)?)(BP|%)', compact)
    if match:
        return 'CD91', round(float(match[1])*(100 if match[2]=='%' else 1),6)
    return None


def unsupported_reason(raw):
    """정확히 파싱하지 않은 산식을 추정값 없이 설명 가능한 사유로 분류한다."""
    compact = re.sub(r'\s+', '', raw).upper()
    if '발행일이후' in compact or re.search(r'이후\d*년', compact):
        return 'FIXED_TO_FLOAT'
    if 'LOOKBACK' in compact or 'KOFR' in compact:
        return 'KOFR_OR_RFR'
    if 'CP' in compact:
        return 'CP_BENCHMARK'
    if 'CD' in compact and ('AAA' not in compact or not re.search(r'민평[34]사', compact)):
        return 'CD_BASIS_UNSPECIFIED'
    if '민평' in compact:
        return 'OTHER_CREDIT_CURVE'
    return 'UNSUPPORTED_FORMULA'


def extract_report(path, url):
    import pdfplumber
    records, failures, periods = [], [], []
    with pdfplumber.open(path) as pdf:
        # 발행현황 부록은 보고서 끝에 있다. 긴 월간 부록이 있어도 마지막 12p를 확인한다.
        for index in range(max(0, len(pdf.pages)-12), len(pdf.pages)):
            page = pdf.pages[index]
            text = page.extract_text() or ''
            if '변동금리채권발행현황' not in re.sub(r'\s+', '', text):
                continue
            period = re.search(r'기간\s*:\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})', text)
            if period:
                periods.append([x.replace('-','') for x in period.groups()])
            # PDF 셀 추출은 종목코드가 null인 경우가 있어, 원문 행의 ISIN을 앵커로 사용한다.
            chunks = re.split(r'(?m)^(?=KR[A-Z0-9]{10}\s)', text)[1:]
            for chunk in chunks:
                line = re.sub(r'\s+', ' ', chunk).strip()
                match = re.match(r'(KR[A-Z0-9]{10})\s+(.+?)\s+(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})\s+(.+?)\s+(\d+)\s+([\d,]+)\s+(.*?)\b(KRW|USD|EUR)\b', line)
                if not match:
                    failures.append({'page':index+1, 'reason':'UNPARSED_ROW', 'text':line})
                    continue
                isin, name_rating, issue, maturity, formula, reset, amount, note, currency = match.groups()
                name = name_rating.rsplit(' ', 1)[0]
                if currency != 'KRW':
                    continue
                parsed = parse_formula(formula)
                if not parsed:
                    failures.append({'isin_code':isin, 'page':index+1, 'reason':unsupported_reason(formula), 'raw_terms':formula})
                    continue
                records.append(dict(isin_code=isin, bond_name=name, issue_date=issue.replace('-',''),
                    maturity_date=maturity.replace('-',''), issue_amount=int(amount.replace(',',''))*100_000_000,
                    benchmark_code=parsed[0], spread_bps=parsed[1], raw_terms=formula,
                    reset_frequency=reset+'M', source_ref=url+'#page='+str(index+1),
                    source_type='KIS_WEEKLY', source_page=index+1))
    return records, failures, periods


def collect(start, end, cached_only=False, db_path=None, backup_database=True):
    datetime.strptime(start, '%Y%m%d'); datetime.strptime(end, '%Y%m%d')
    if start > end:
        raise ValueError('시작일은 종료일보다 늦을 수 없습니다.')
    folder = ROOT/'data'/'kis_reports'
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder/f'manifest_{start}_{end}.json'
    if cached_only:
        reports = json.loads(manifest_path.read_text(encoding='utf-8'))
    else:
        reports = discover_reports(start, end)
        if not reports:
            raise RuntimeError('기간 내 공개 보고서가 없습니다.')
        manifest_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    conn = get_connection(db_path)
    init_issue_schema(conn)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup = ROOT/f'bond_before_issue_terms_{stamp}.db' if backup_database else None
    if backup:
        with sqlite3.connect(backup) as backup_conn:
            conn.backup(backup_conn)
    totals, logs = Counter(), []
    for report in reports:
        entry = dict(report)
        try:
            path = folder/(report['id']+'.pdf')
            if not path.exists():
                if cached_only:
                    raise FileNotFoundError(path)
                blob = request(report['url'])
                if not blob.startswith(b'%PDF'):
                    raise ValueError('PDF 응답이 아닙니다.')
                path.write_bytes(blob)
                time.sleep(0.2)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            cache_path = folder/(report['id']+'.json')
            cache = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
            if cache.get('sha256') == digest and cache.get('version') == PARSER_VERSION:
                rows, skipped, periods = cache['rows'], cache['skipped'], cache['periods']
            else:
                rows, skipped, periods = extract_report(path, report['url'])
                cache_path.write_text(json.dumps(dict(version=PARSER_VERSION,sha256=digest,rows=rows,
                    skipped=skipped,periods=periods),ensure_ascii=False,indent=2),encoding='utf-8')
            if not periods:
                raise ValueError('최초 발행현황 부록의 기간을 확인하지 못했습니다. PDF를 검토하세요.')
            statuses = Counter()
            with conn:
                for row in rows:
                    if not start <= row['issue_date'] <= end:
                        continue
                    statuses[save_issue_observation(conn, row)] += 1
                save_source_report(conn, report, PARSER_VERSION, sha256=digest,
                                   periods=periods, rows_parsed=len(rows), failures=skipped)
            totals.update(statuses)
            entry.update(statuses=dict(statuses), skipped=skipped, periods=periods,
                         sha256=digest)
            print(json.dumps({'report':report['report_date'],'counts':dict(statuses),'skipped':len(skipped)}), flush=True)
        except Exception as exc:
            conn.rollback()
            entry['error'] = f'{type(exc).__name__}: {exc}'
            with conn:
                save_source_report(conn, report, PARSER_VERSION, error=entry['error'])
            totals['REPORT_FAILED'] += 1
            print(json.dumps({'report':report['report_date'],'error':entry['error']},ensure_ascii=True),flush=True)
        logs.append(entry)
    conn.close()
    result = dict(requested_start=start, requested_end=end, totals=dict(totals), reports=logs,
                  backup=str(backup), completed_at=datetime.now().isoformat(timespec='seconds'))
    (ROOT/'data'/'issuance_collection_report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='은행 FRN 최초 발행 가산금리 수집')
    parser.add_argument('--start',required=True)
    parser.add_argument('--end',required=True)
    parser.add_argument('--cached-only',action='store_true')
    args=parser.parse_args()
    print(json.dumps(collect(args.start,args.end,args.cached_only)['totals'],ensure_ascii=True))
