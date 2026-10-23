"""현재 채권 마스터의 은행 FRN 연도별 커버리지 점검."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re

from db import get_connection
from frn_analysis import canonical_issuer
from kis_issuance import extract_report, request


BANKS = ['국민은행', '신한은행', '하나은행', '우리은행', '농협은행']
ROOT = Path(__file__).resolve().parent


def _prepare_kis_report(report):
    folder = ROOT / 'data' / 'kis_reports'
    path = folder / f"{report['id']}.pdf"
    try:
        if not path.exists():
            content = request(report['url'])
            if not content.startswith(b'%PDF'):
                raise ValueError('PDF 응답이 아닙니다.')
            path.write_bytes(content)
        rows, skipped, periods = extract_report(path, report['url'])
        return {**report, 'rows': rows, 'skipped': skipped, 'periods': periods}
    except Exception as exc:
        return {**report, 'rows': [], 'skipped': [], 'periods': [],
                'error': f'{type(exc).__name__}: {exc}'}


def audit_kis_2024():
    manifest = json.loads((ROOT/'data'/'kis_reports'/'manifest_20220101_20261023.json').read_text(encoding='utf-8'))
    reports = [report for report in manifest if report['report_date'].startswith('2024')]
    conn = get_connection()
    findings = []
    unsupported = []
    report_log = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for report in pool.map(_prepare_kis_report, reports):
            report_log.append({key: report.get(key) for key in ('id','report_date','url','periods','error') if report.get(key)})
            for row in report['rows']:
                master = conn.execute("""
                    SELECT i.issuer_name,b.rate_type_code,b.rate_type_name,b.bond_name,b.issue_amount
                    FROM bond_master b LEFT JOIN issuer_mapping i ON i.issuer_id=b.issuer_id
                    WHERE b.isin_code=?
                """, (row['isin_code'],)).fetchone()
                issuer = canonical_issuer(master['issuer_name']) if master else None
                if issuer not in BANKS:
                    continue
                findings.append({
                    **{key: row[key] for key in ('isin_code','issue_date','maturity_date','issue_amount','benchmark_code','spread_bps','raw_terms','source_ref')},
                    'issuer': issuer,
                    'master_rate_type_code': master['rate_type_code'],
                    'master_rate_type_name': master['rate_type_name'],
                    'master_amount_matches': int(master['issue_amount']) == int(row['issue_amount']),
                })
            for item in report['skipped']:
                isin = item.get('isin_code')
                if not isin:
                    match = re.match(r'(KR[A-Z0-9]{10})', item.get('text',''))
                    isin = match.group(1) if match else None
                if not isin:
                    continue
                master = conn.execute("""
                    SELECT i.issuer_name,b.rate_type_code,b.rate_type_name,b.bond_name,b.issue_amount,b.issue_date
                    FROM bond_master b LEFT JOIN issuer_mapping i ON i.issuer_id=b.issuer_id
                    WHERE b.isin_code=?
                """, (isin,)).fetchone()
                issuer = canonical_issuer(master['issuer_name']) if master else None
                if issuer not in BANKS:
                    continue
                unsupported.append({
                    'isin_code': isin, 'issuer': issuer,
                    'issue_date': master['issue_date'], 'issue_amount': master['issue_amount'],
                    'bond_name': master['bond_name'], 'reason': item.get('reason'),
                    'raw_terms': item.get('raw_terms', item.get('text','')),
                    'master_rate_type_code': master['rate_type_code'],
                    'master_rate_type_name': master['rate_type_name'],
                    'source_ref': report['url']+'#page='+str(item.get('page','')),
                })
    conn.close()
    unique = {row['isin_code']: row for row in findings}
    findings = sorted(unique.values(), key=lambda row:(row['issue_date'],row['issuer'],row['isin_code']))
    unsupported = sorted({row['isin_code']: row for row in unsupported}.values(),
                         key=lambda row:(row['issue_date'],row['issuer'],row['isin_code']))
    summary = {}
    for bank in BANKS:
        rows = [row for row in findings if row['issuer'] == bank]
        other_rows = [row for row in unsupported if row['issuer'] == bank]
        summary[bank] = {
            'count': len(rows),
            'amount_100m': sum(row['issue_amount'] for row in rows)/1e8,
            'unsupported_formula_count': len(other_rows),
            'unsupported_formula_amount_100m': sum(row['issue_amount'] for row in other_rows)/1e8,
            'master_labeled_fixed': sum(row['master_rate_type_code'] == '1' for row in rows),
            'master_labeled_floating': sum(row['master_rate_type_code'] == '2' for row in rows),
        }
    result = {
        'year': 2024,
        'source_reports': len(reports),
        'reports_with_frn_appendix': sum(bool(log.get('periods')) for log in report_log),
        'errors': sum(bool(log.get('error')) for log in report_log),
        'summary': summary,
        'totals': {
            'count': len(findings),
            'amount_100m': sum(row['issue_amount'] for row in findings)/1e8,
            'master_labeled_fixed': sum(row['master_rate_type_code'] == '1' for row in findings),
            'master_labeled_floating': sum(row['master_rate_type_code'] == '2' for row in findings),
        },
        'findings': findings,
        'unsupported_target_bank_rows': unsupported,
        'reports': report_log,
    }
    output = ROOT/'data'/'frn_coverage_audit_2024.json'
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('source_reports','reports_with_frn_appendix','errors','summary','totals')}, ensure_ascii=False, indent=2))
    print(f'AUDIT_FILE={output}')


def main():
    conn = get_connection()
    placeholders = ','.join('?' for _ in BANKS)
    print('RATE_BREAKDOWN')
    rows = conn.execute(f"""
        SELECT substr(b.issue_date,1,4) AS year, i.issuer_name,
               coalesce(b.rate_type_code,'NULL') AS rate_code,
               coalesce(b.rate_type_name,'NULL') AS rate_name,
               count(*) AS count, sum(b.issue_amount)/1e8 AS amount_100m
        FROM bond_master b LEFT JOIN issuer_mapping i ON i.issuer_id=b.issuer_id
        WHERE i.issuer_name IN ({placeholders})
          AND b.issue_date BETWEEN '20220101' AND '20261023'
        GROUP BY 1,2,3,4 ORDER BY 1,2,3
    """, BANKS).fetchall()
    for row in rows:
        print(dict(row))

    print('2024_BANKLIKE_ISSUERS')
    rows = conn.execute("""
        SELECT i.issuer_name, count(*) AS count, sum(b.issue_amount)/1e8 AS amount_100m
        FROM bond_master b LEFT JOIN issuer_mapping i ON i.issuer_id=b.issuer_id
        WHERE b.issue_date BETWEEN '20240101' AND '20241231'
          AND (i.issuer_name LIKE '%은행%' OR b.bond_name LIKE '%은행%')
        GROUP BY 1 ORDER BY count DESC
    """).fetchall()
    for row in rows:
        print(dict(row))

    print('2024_TARGET_NAME_MATCHES')
    rows = conn.execute("""
        SELECT b.issue_date, i.issuer_name, b.bond_name, b.rate_type_code,
               b.rate_type_name, b.issue_amount/1e8 AS amount_100m
        FROM bond_master b LEFT JOIN issuer_mapping i ON i.issuer_id=b.issuer_id
        WHERE b.issue_date BETWEEN '20240101' AND '20241231'
          AND (b.bond_name LIKE '%국민은행%' OR b.bond_name LIKE '%하나은행%'
               OR b.bond_name LIKE '%농업금융%' OR b.bond_name LIKE '%농협은행%')
        ORDER BY b.issue_date
    """).fetchall()
    for row in rows:
        print(dict(row))

    print('YEAR_TOTAL_ALL_BONDS')
    for row in conn.execute("""
        SELECT substr(issue_date,1,4) AS year, count(*) AS count
        FROM bond_master WHERE issue_date BETWEEN '20220101' AND '20261023'
        GROUP BY 1 ORDER BY 1
    """):
        print(dict(row))
    conn.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--kis-2024', action='store_true')
    args = parser.parse_args()
    audit_kis_2024() if args.kis_2024 else main()
