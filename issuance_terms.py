"""발행 당시 가산금리의 출처별 이력·수집 근거·검증된 대표값."""
import hashlib
import json
import math
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS frn_issue_observations (
    observation_id INTEGER PRIMARY KEY,
    isin_code TEXT NOT NULL REFERENCES bond_master(isin_code),
    issue_date TEXT NOT NULL,
    maturity_date TEXT NOT NULL,
    benchmark_code TEXT NOT NULL,
    spread_bps REAL NOT NULL,
    raw_terms TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_page INTEGER,
    reset_frequency TEXT,
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(isin_code, source_ref, raw_terms)
);
CREATE TABLE IF NOT EXISTS frn_issue_terms (
    isin_code TEXT PRIMARY KEY REFERENCES bond_master(isin_code),
    issue_date TEXT NOT NULL,
    maturity_date TEXT NOT NULL,
    benchmark_code TEXT NOT NULL,
    spread_bps REAL NOT NULL,
    confidence_status TEXT NOT NULL,
    raw_terms TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_page INTEGER,
    reset_frequency TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS frn_source_reports (
    report_id TEXT PRIMARY KEY,
    report_date TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    parser_version INTEGER NOT NULL,
    sha256 TEXT,
    status TEXT NOT NULL,
    periods_json TEXT NOT NULL DEFAULT '[]',
    rows_parsed INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS frn_source_failures (
    failure_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES frn_source_reports(report_id),
    isin_code TEXT,
    source_page INTEGER,
    reason TEXT NOT NULL,
    raw_text TEXT,
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_frn_source_failures_isin
    ON frn_source_failures(isin_code);
"""


def init_issue_schema(conn):
    conn.executescript(SCHEMA)


def save_source_report(conn, report, parser_version, *, sha256=None, periods=None,
                       rows_parsed=0, failures=None, error=None):
    """보고서 단위 수집 결과와 행 단위 제외 사유를 재현 가능하게 저장한다."""
    failures = failures or []
    status = 'FAILED' if error else 'PARSED'
    conn.execute("""INSERT INTO frn_source_reports
        (report_id,report_date,source_type,source_url,parser_version,sha256,status,
         periods_json,rows_parsed,rows_skipped,error,collected_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        ON CONFLICT(report_id) DO UPDATE SET
        report_date=excluded.report_date,source_type=excluded.source_type,
        source_url=excluded.source_url,parser_version=excluded.parser_version,
        sha256=excluded.sha256,status=excluded.status,periods_json=excluded.periods_json,
        rows_parsed=excluded.rows_parsed,rows_skipped=excluded.rows_skipped,
        error=excluded.error,collected_at=CURRENT_TIMESTAMP""",
        (str(report['id']), report['report_date'], 'KIS_WEEKLY', report['url'],
         parser_version, sha256, status, json.dumps(periods or [], ensure_ascii=False),
         rows_parsed, len(failures), error))
    conn.execute("DELETE FROM frn_source_failures WHERE report_id=?", (str(report['id']),))
    for item in failures:
        raw = item.get('raw_terms') or item.get('text') or ''
        payload = '|'.join((str(report['id']), str(item.get('page', '')),
                            str(item.get('isin_code', '')), item['reason'], raw))
        failure_id = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        conn.execute("""INSERT OR REPLACE INTO frn_source_failures
            (failure_id,report_id,isin_code,source_page,reason,raw_text)
            VALUES (?,?,?,?,?,?)""",
            (failure_id, str(report['id']), item.get('isin_code'), item.get('page'),
             item['reason'], raw))


def load_source_coverage(conn):
    """대시보드가 후보별 미확보 사유를 붙일 수 있는 작은 데이터 묶음을 반환한다."""
    import pandas as pd
    reports = pd.DataFrame([dict(r) for r in conn.execute(
        "SELECT * FROM frn_source_reports ORDER BY report_date")])
    failures = pd.DataFrame([dict(r) for r in conn.execute(
        "SELECT * FROM frn_source_failures ORDER BY report_id,source_page")])
    conflicts = {r[0] for r in conn.execute(
        "SELECT isin_code FROM frn_issue_terms WHERE confidence_status='CONFLICT'")}
    return reports, failures, conflicts


def save_issue_observation(conn, row):
    """ISIN·발행일·만기·금액을대조. 다른 출처의 다른 조건은 CONFLICT로 격리."""
    master = conn.execute(
        "SELECT b.issue_date,b.maturity_date,b.issue_amount,i.issuer_name FROM bond_master b "
        "LEFT JOIN issuer_mapping i ON i.issuer_id=b.issuer_id WHERE b.isin_code=?",
        (row['isin_code'],),
    ).fetchone()
    if master is None:
        return 'UNKNOWN_ISIN'
    from frn_analysis import canonical_issuer
    if canonical_issuer(master[3]) is None:
        return 'NOT_BANK'
    if (master[0], master[1]) != (row['issue_date'], row['maturity_date']):
        return 'DATE_MISMATCH'
    if int(master[2]) != int(row['issue_amount']):
        return 'AMOUNT_MISMATCH'
    if not math.isfinite(row['spread_bps']):
        raise ValueError('가산금리는 유한한 숫자여야 합니다.')
    for key in ('issue_date', 'maturity_date'):
        datetime.strptime(row[key], '%Y%m%d')
    values = [row[k] for k in ('isin_code','issue_date','maturity_date','benchmark_code',
                              'spread_bps','raw_terms','source_ref','source_type')]
    values += [row.get('source_page'), row.get('reset_frequency')]
    conn.execute("""INSERT OR IGNORE INTO frn_issue_observations
        (isin_code,issue_date,maturity_date,benchmark_code,spread_bps,raw_terms,
         source_ref,source_type,source_page,reset_frequency) VALUES (?,?,?,?,?,?,?,?,?,?)""", values)
    evidence = conn.execute(
        "SELECT DISTINCT benchmark_code,spread_bps FROM frn_issue_observations WHERE isin_code=?",
        (row['isin_code'],),
    ).fetchall()
    status = 'CONFIRMED' if len(evidence) == 1 else 'CONFLICT'
    # 최초 대표값을 유지한다. 후속 수집·리셋값으로 덮어쓰지 않는다.
    conn.execute("""INSERT INTO frn_issue_terms
        (isin_code,issue_date,maturity_date,benchmark_code,spread_bps,raw_terms,
         source_ref,source_type,source_page,reset_frequency,confidence_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(isin_code) DO UPDATE SET
        confidence_status=excluded.confidence_status,updated_at=CURRENT_TIMESTAMP""", values + [status])
    return status


def load_issue_terms(conn):
    import pandas as pd
    from frn_analysis import (add_frn_structure, canonical_issuer, ISSUER_GROUP_BY_CANONICAL,
                              MATURITY_BINS_MONTHS, MATURITY_LABELS)
    rows = conn.execute("""SELECT t.*,b.bond_name,b.issue_amount,b.rate_type_code,
        b.rate_type_name,i.issuer_name
        FROM frn_issue_terms t JOIN bond_master b ON b.isin_code=t.isin_code
        LEFT JOIN issuer_mapping i ON i.issuer_id=b.issuer_id WHERE b.currency='KRW'""").fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df['issuer'] = df['issuer_name'].map(canonical_issuer)
    df = df[df.issuer.notna()].copy()
    df['issuer_group'] = df.issuer.map(ISSUER_GROUP_BY_CANONICAL)
    df['issue_dt'] = pd.to_datetime(df.issue_date, format='%Y%m%d')
    df['original_tenor_months'] = (pd.to_datetime(df.maturity_date, format='%Y%m%d')-df.issue_dt).dt.days/(365.25/12)
    df['maturity_bucket'] = pd.cut(df.original_tenor_months, MATURITY_BINS_MONTHS, labels=MATURITY_LABELS, right=False)
    df['issue_amount_억원'] = df.issue_amount / 1e8
    return add_frn_structure(df)


BENCHMARK_LABELS = {
    'CD1M_M3_AAA': '민평3사 AAA CD 1개월',
    'CD1M_M4_AAA': '민평4사 AAA CD 1개월',
    'CD91': 'CD 91일물',
}
