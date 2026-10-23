"""금융위 채권 API 전수 재수집 후 검증된 DB만 교체한다.

실행: python refresh_db.py
진단: python refresh_db.py --audit-only
"""
import argparse
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from config import DB_PATH, load_api_key, load_ecos_api_key
from db import get_connection, init_db
from etl import etl_fsc_incremental_sync
from reference_rates import fetch_ecos_cd91, save_reference_rates

ROOT = Path(__file__).resolve().parent
BANK_NAMES = (
    "한국산업은행", "한국수출입은행", "중소기업은행", "농협은행", "수협은행",
    "국민은행", "신한은행", "하나은행", "우리은행", "한국씨티은행",
    "한국스탠다드차타드은행", "부산은행", "경남은행", "광주은행",
    "전북은행", "제주은행", "대구은행", "iM뱅크", "카카오뱅크", "케이뱅크", "토스뱅크",
)
BUCKETS = ("6개월", "9개월", "1년", "1.25년", "1.5년", "1.75년", "2년", "3년", "5년", "10년")
BOUNDS = (7.5, 10.5, 13.5, 16.5, 19.5, 22.5, 30, 48, 90)


def _owned_path(path):
    resolved = Path(path).resolve()
    if resolved.parent != ROOT:
        raise ValueError(f"DB 파일은 프로젝트 폴더 안에 있어야 합니다: {resolved}")
    return resolved


def audit_db(path):
    """저장된 실자료의 발행일·FRN 필드 적재율을 읽기 전용으로 집계한다."""
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        overall = dict(conn.execute("""
            SELECT COUNT(*) AS bonds, MAX(issue_date) AS latest_issue_date,
                   SUM(CASE WHEN coupon_rate IS NOT NULL THEN 1 ELSE 0 END) AS coupon_rows,
                   SUM(CASE WHEN rate_type_name IS NOT NULL THEN 1 ELSE 0 END) AS rate_type_rows
            FROM bond_master
        """).fetchone())
        banks = []
        for row in conn.execute("""
            SELECT i.issuer_name, COUNT(*) AS bonds,
                   SUM(CASE WHEN b.rate_type_code IN ('2','3') OR b.rate_type_name LIKE '%변동%'
                                OR b.bond_name LIKE '%FRN%' OR b.bond_name LIKE '%(변)%'
                                OR b.bond_name LIKE '%-변%' OR b.bond_name LIKE '%변동%' OR b.bond_name LIKE '%CD+%'
                            THEN 1 ELSE 0 END) AS frn_candidates,
                   SUM(CASE WHEN b.coupon_rate IS NOT NULL THEN 1 ELSE 0 END) AS coupon_rows,
                   MAX(b.issue_date) AS latest_issue_date
            FROM bond_master b JOIN issuer_mapping i ON i.issuer_id=b.issuer_id
            WHERE b.currency='KRW'
            GROUP BY i.issuer_name ORDER BY i.issuer_name
        """):
            if any(name in row["issuer_name"] for name in BANK_NAMES):
                banks.append(dict(row))
        bucket_counts = {}
        for row in conn.execute("""
            SELECT i.issuer_name, b.issue_date, b.maturity_date
            FROM bond_master b JOIN issuer_mapping i ON i.issuer_id=b.issuer_id
            WHERE b.currency='KRW' AND
                  (b.rate_type_name LIKE '%변동%' OR b.bond_name LIKE '%FRN%'
                   OR b.bond_name LIKE '%변동%' OR b.bond_name LIKE '%CD+%')
        """):
            issuer = row["issuer_name"]
            if not any(name in issuer for name in BANK_NAMES):
                continue
            try:
                issue = datetime.strptime(row["issue_date"], "%Y%m%d")
                maturity = datetime.strptime(row["maturity_date"], "%Y%m%d")
                months = (maturity - issue).days / (365.25 / 12)
                if months <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            bucket = next((BUCKETS[i] for i, edge in enumerate(BOUNDS) if months < edge), BUCKETS[-1])
            bucket_counts.setdefault(issuer, {})[bucket] = bucket_counts.setdefault(issuer, {}).get(bucket, 0) + 1
        terms_total = conn.execute("SELECT COUNT(*) FROM frn_terms").fetchone()[0]
        terms = conn.execute("SELECT COUNT(*) FROM frn_terms WHERE confidence_status IN ('CONFIRMED','INFERRED')").fetchone()[0]
        variable_bonds = conn.execute("SELECT COUNT(*) FROM bond_master WHERE currency='KRW' AND rate_type_code IN ('2','3')").fetchone()[0]
        rates = conn.execute("SELECT benchmark_code,COUNT(*) FROM reference_rate_daily GROUP BY benchmark_code").fetchall()
        sync = conn.execute("SELECT meta_val FROM etl_metadata WHERE meta_key='last_fsc_sync_date'").fetchone()
        issue_table = conn.execute("SELECT 1 FROM sqlite_master WHERE name='frn_issue_terms'").fetchone()
        issue_terms = conn.execute("SELECT COUNT(*) FROM frn_issue_terms WHERE confidence_status='CONFIRMED'").fetchone()[0] if issue_table else 0
    return {**overall, "frn_candidates": variable_bonds, "frn_terms_total": terms_total,
            "initial_issue_spreads": issue_terms,
            "verified_frn_terms": terms,
            "reference_rates": {r[0]: r[1] for r in rates},
            "last_fsc_sync_date": sync[0] if sync else None,
            "banks": banks, "frn_maturity_buckets": bucket_counts}


def _backup_db(source, destination):
    with closing(sqlite3.connect(source)) as old, closing(sqlite3.connect(destination)) as backup:
        old.backup(backup)


def refresh(include_ecos=True, include_issue_terms=True, issue_terms_start="20240101"):
    original = _owned_path(DB_PATH)
    if not original.exists():
        raise FileNotFoundError(original)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging = _owned_path(ROOT / f"bond_staging_{stamp}.db")
    backup = _owned_path(ROOT / f"bond_backup_{stamp}.db")
    if staging.exists() or backup.exists():
        raise FileExistsError("해당 시각의 임시 DB/백업이 이미 있습니다. 잠시 후 다시 실행하세요.")
    before = audit_db(original)
    _backup_db(original, staging)
    warnings = []
    promoted = False
    try:
        init_db(staging)
        conn = get_connection(staging)
        try:
            public_key = load_api_key()
            received, _ = etl_fsc_incremental_sync(public_key, conn, force_full=True)
            if include_ecos:
                key = load_ecos_api_key()
                if key:
                    try:
                        today = datetime.now().strftime("%Y%m%d")
                        rates = fetch_ecos_cd91(key, "20200101", today)
                        if not rates:
                            warnings.append("ECOS CD91 반환 행이 없습니다.")
                        else:
                            save_reference_rates(conn, rates)
                    except Exception as exc:
                        # FSC 종목 재수집은 유효할 수 있다. 금리 실패는 별도로 기록한다.
                        detail = str(exc).replace(key, "[REDACTED]")
                        warnings.append(f"ECOS CD91 적재 실패: {type(exc).__name__}: {detail}")
                else:
                    warnings.append("ECOS_API_KEY가 없어 CD91 수집을 건너뛰었습니다.")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        if include_issue_terms:
            try:
                from kis_issuance import collect
                today = datetime.now().strftime('%Y%m%d')
                datetime.strptime(issue_terms_start, "%Y%m%d")
                result = collect(issue_terms_start, today, db_path=staging, backup_database=False)
                if result['totals'].get('REPORT_FAILED'):
                    warnings.append('일부 최초 발행조건 보고서 수집 실패. issuance_collection_report.json을 확인하세요.')
            except Exception as exc:
                warnings.append(f'최초 발행조건 수집 실패(기존 이력 유지): {type(exc).__name__}: {exc}')
        after = audit_db(staging)
        if received < max(1, int(before["bonds"] * 0.90)):
            raise RuntimeError(f"수신 종목 수가 기존 DB 대비 급감했습니다: {received:,} / {before['bonds']:,}")
        if after["bonds"] < before["bonds"] or after["latest_issue_date"] < before["latest_issue_date"]:
            raise RuntimeError("검증 DB의 종목 수 또는 최신 발행일이 기존보다 후퇴했습니다.")
        if after["initial_issue_spreads"] < before["initial_issue_spreads"]:
            raise RuntimeError(
                "검증 DB의 확정 최초 발행조건이 감소했습니다: "
                f"{after['initial_issue_spreads']:,} / {before['initial_issue_spreads']:,}"
            )
        if after["last_fsc_sync_date"] != datetime.now().strftime("%Y%m%d"):
            raise RuntimeError("금융위 전수 수집 완료 워터마크가 기록되지 않았습니다.")
        if after["coupon_rows"] == 0 or after["rate_type_rows"] == 0:
            warnings.append("API 응답에 표면금리 또는 금리유형이 없어 가산금리 분석 표본이 늘지 않았습니다.")
        # 모든 검증 후 원본을 별도 백업한다. Windows에서 DB 사용 중이면 교체가 실패하며 백업은 남는다.
        _backup_db(original, backup)
        with closing(sqlite3.connect(staging)) as stage:
            stage.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            stage.execute("PRAGMA journal_mode=DELETE").fetchone()
        with closing(sqlite3.connect(original)) as current:
            current.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            current.execute("PRAGMA journal_mode=DELETE").fetchone()
        os.replace(staging, original)
        promoted = True
        report = {"completed_at": datetime.now().isoformat(timespec="seconds"),
                  "backup": str(backup), "before": before, "after": after, "warnings": warnings}
        report_path = ROOT / "data" / f"refresh_report_{stamp}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"DB 교체 완료. 백업: {backup.name} / 검증 보고서: {report_path}")
        print(f"종목 {after['bonds']:,}건, 최신 발행일 {after['latest_issue_date']}, "
              f"표면금리 {after['coupon_rows']:,}건, FRN 후보 {after['frn_candidates']:,}건, "
              f"최초 발행 가산금리 {after['initial_issue_spreads']:,}건")
        for warning in warnings:
            print("주의:", warning)
        return report
    except Exception:
        if promoted:
            print(f"DB는 교체됐지만 보고서 저장에 실패했습니다. 백업: {backup}")
        else:
            print(f"DB 교체 전 실패했습니다. 원본 유지. 검토용 임시 DB: {staging}")
        raise


def main():
    parser = argparse.ArgumentParser(description="공공데이터 API 전수 재수집과 DB 검증")
    parser.add_argument("--audit-only", action="store_true", help="네트워크 없이 기존 DB만 집계")
    parser.add_argument("--skip-ecos", action="store_true", help="한국은행 CD91 수집 생략")
    parser.add_argument("--skip-ksd", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip-issue-terms", action="store_true", help="최초 발행조건 보고서 수집 생략")
    parser.add_argument("--issue-terms-start", default="20240101",
                        help="최초 발행조건 재검증 시작일(기본 20240101)")
    args = parser.parse_args()
    if args.audit_only:
        print(json.dumps(audit_db(_owned_path(DB_PATH)), ensure_ascii=False, indent=2))
    else:
        refresh(include_ecos=not args.skip_ecos, include_issue_terms=not args.skip_issue_terms,
                issue_terms_start=args.issue_terms_start)


if __name__ == "__main__":
    main()
