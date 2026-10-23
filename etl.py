"""
ETL 파이프라인 모듈 (증분 수집 Incremental ETL 관리 적용)
- FRN 대시보드 기본 실행은 FSC 채권 마스터만 갱신
- FSC V2 API: 대한민국 개별 채권 실데이터 증분/전수 수집
- 한번 수집이 완료된 날짜 내역은 유지하고, 신규 발행일자/기준일자 채권만 효율적으로 증분 수집
"""
from datetime import datetime
import math
from config import load_api_key
from classifier import classify_sector, extract_issuer_name
from collector import (
    fetch_bond_kind_insetl_stat,
    fetch_fsc_bond_items_all,
)
from db import (
    init_db, get_connection,
    upsert_issuer, upsert_bond_master, insert_supply_flow,
    upsert_settlement_stat,
    query_table_counts, get_meta_value, set_meta_value
)


def _optional_float(value):
    """API의 미제공 표면금리 표기('-', 공백 등)를 결측으로 취급한다."""
    if value is None or str(value).strip() in {"", "-", "N/A", "null"}:
        return None
    number = float(str(value).replace(",", ""))
    return number if math.isfinite(number) else None


def etl_settlement_stat(api_key, conn, year):
    """기관결제대금 현황 수집."""
    periods = [(f"{year}0101", f"{year}0630"), (f"{year}0701", f"{year}1231")]
    count = 0
    for begin, expiry in periods:
        try:
            print(f"  [ETL] 기관결제대금 현황 ({begin} ~ {expiry})")
            data = fetch_bond_kind_insetl_stat(api_key, begin, expiry)
            for row in data:
                upsert_settlement_stat(
                    conn,
                    std_yymm=row.get("stdYymm", ""),
                    bond=int(row.get("bondSetlCost", 0) or 0),
                    cd=int(row.get("cdSetlCost", 0) or 0),
                    cp=int(row.get("cpSetlCost", 0) or 0),
                    stb=int(row.get("stbSetlCost", 0) or 0),
                )
                count += 1
        except Exception as e:
            print(f"    ⚠️ 실패 ({begin}~{expiry}): {e}")
    conn.commit()
    return count


def etl_fsc_incremental_sync(api_key, conn, force_full=False):
    """
    금융위원회 V2 API 증분 수집 (Incremental Sync).
    - basDt 없이 전체 이력(85,000+건) 수집: 이미 만기된 과거 채권 포함
    - 수집 완료된 당일 재실행 시에는 API 호출 없이 스킵 (워터마크 관리)
    """
    today_str = datetime.now().strftime("%Y%m%d")
    last_sync_date = get_meta_value(conn, "last_fsc_sync_date")

    print(f"\n--- 1. 금융위원회 V2 API 개별 채권 전수 수집 (Incremental ETL) ---")
    print(f"  마지막 완료 수집일: {last_sync_date or '없음(최초 실행)'} | 오늘: {today_str}")

    # 이미 오늘 수집이 완료되었고 강제 수집이 아니면 스킵
    if last_sync_date == today_str and not force_full:
        print(f"  ℹ️ 오늘({today_str}) 기준 수집이 이미 완료되어 있습니다. (Incremental Skip)")
        return 0, 0

    # basDt 없이 호출 → 전체 이력(이미 만기된 채권 포함) 85,000+건 수집
    items = fetch_fsc_bond_items_all(api_key, bas_dt=None, rows_per_page=1000)
    print(f"  [FSC API] 총 {len(items):,}건 수신 완료. DB 증분 UPSERT 적재 시작...")

    total_bonds = 0
    total_flows = 0

    for item in items:
        isin = item.get("isinCd", "")
        bond_name = item.get("isinCdNm", "") or item.get("bondIsurNm", "")
        if not isin or not bond_name:
            continue

        # ── 정규식 자동 섹터 분류 ──
        sector_code, sector_label = classify_sector(bond_name)
        issuer_name = item.get("bondIsurNm", "") or extract_issuer_name(bond_name)
        issuer_id = item.get("crno", "UNKNOWN")

        # 1. issuer_mapping 적재
        upsert_issuer(conn, issuer_id=issuer_id, name=issuer_name, sector=sector_code)

        # 2. bond_master 적재
        issue_date = item.get("bondIssuDt", "")
        maturity_date = item.get("bondExprDt", "")
        issue_amt = int(item.get("bondIssuAmt", 0) or 0)

        upsert_bond_master(
            conn,
            isin=isin,
            issuer_id=issuer_id,
            name=bond_name,
            bond_type=item.get("scrsItmsKcdNm", "채권"),
            issue_date=issue_date,
            maturity_date=maturity_date,
            issue_amount=issue_amt,
            currency=item.get("bondIssuCurCd", "KRW"),
            coupon_rate=_optional_float(item.get("bondSrfcInrt")),
            rate_type_code=item.get("irtChngDcd"),
            rate_type_name=item.get("irtChngDcdNm"),
            interest_type_code=item.get("bondIntTcd"),
            interest_type_name=item.get("bondIntTcdNm"),
        )
        total_bonds += 1

        # 3. bond_supply_flow 적재 (발행 이벤트)
        if issue_date and issue_amt > 0:
            insert_supply_flow(conn, isin, issue_date, "ISSUE", issue_amt)
            total_flows += 1

        # 4. bond_supply_flow 적재 (만기 이벤트)
        if maturity_date and issue_amt > 0:
            insert_supply_flow(conn, isin, maturity_date, "MATURITY", issue_amt)
            total_flows += 1

    # 마지막 성공 수집 워터마크 저장
    set_meta_value(conn, "last_fsc_sync_date", today_str)
    set_meta_value(conn, "last_fsc_total_count", total_bonds)
    conn.commit()
    print(f"  ✅ 개별 채권 증분 수집 및 완료 워터마크({today_str}) 저장 완료 (종목:{total_bonds}건, 수급이벤트:{total_flows}건)")
    return total_bonds, total_flows


def run_full_etl():
    """전체 ETL 파이프라인 실행."""
    api_key = load_api_key()
    init_db()
    conn = get_connection()

    # 2. 금융위원회 V2 API 개별 채권 증분 수집 (Incremental Ingestion)
    etl_fsc_incremental_sync(api_key, conn, force_full=False)

    print("\n" + "=" * 60)
    print("[ETL 완료 - 100% 공공데이터 API 실데이터] 테이블별 레코드 수:")
    counts = query_table_counts(conn)
    for table, cnt in counts.items():
        print(f"  {table}: {cnt}건")

    conn.close()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    run_full_etl()
