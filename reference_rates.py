"""CD/KOFR 기준금리 수집과 FRN 가산금리 데이터 적재 도구."""
import argparse
import csv
import io
import json
import math
from datetime import datetime
from pathlib import Path
import urllib.parse
import urllib.request

from config import load_ecos_api_key
from db import init_db, get_connection, upsert_reference_rate, upsert_frn_term

ECOS_BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"
ECOS_CD91_STAT_CODE = "817Y002"
ECOS_CD91_ITEM_CODE = "010502000"


def fetch_ecos_cd91(api_key, start_date, end_date):
    """한국은행 ECOS에서 일별 CD 91일 금리를 조회한다."""
    if datetime.strptime(start_date, "%Y%m%d") > datetime.strptime(end_date, "%Y%m%d"):
        raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
    result = []
    start, page_size = 1, 1000
    while True:
        path = "/".join([
            api_key, "json", "kr", str(start), str(start + page_size - 1),
            ECOS_CD91_STAT_CODE, "D", start_date, end_date, ECOS_CD91_ITEM_CODE,
        ])
        url = f"{ECOS_BASE_URL}/{urllib.parse.quote(path, safe='/')}"
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        block = payload.get("StatisticSearch", {})
        if not block:
            message = payload.get("RESULT", {}).get("MESSAGE", "ECOS 응답에 StatisticSearch가 없습니다.")
            raise RuntimeError(message)
        rows = block.get("row", [])
        total = int(block.get("list_total_count", 0))
        if not rows and start <= total:
            raise RuntimeError("ECOS 페이지가 비어 있어 적재를 중단합니다.")
        for row in rows:
            label = " ".join(str(row.get(k, "")) for k in ("ITEM_NAME1", "ITEM_NAME2", "ITEM_NAME3"))
            if "CD" not in label.upper() or not ("91" in label or "3개월" in label):
                raise RuntimeError(f"ECOS 항목이 CD 91일물인지 확인할 수 없습니다: {label}")
            date = str(row.get("TIME", "")).replace("-", "")
            value = row.get("DATA_VALUE")
            if len(date) == 8 and value not in (None, ""):
                result.append({"rate_date": date, "rate_value": float(value)})
        if start + len(rows) > total:
            break
        start += len(rows)
    return result


def save_reference_rates(conn, rows, benchmark_code="CD91", source="BOK_ECOS"):
    count = 0
    for row in rows:
        upsert_reference_rate(
            conn, benchmark_code, row["rate_date"], row["rate_value"],
            row.get("source", source), row.get("published_at")
        )
        count += 1
    conn.commit()
    return count


def _read_csv_rows(source):
    """파일 경로, Streamlit UploadedFile, 텍스트 스트림을 공통 처리한다."""
    if hasattr(source, "getvalue"):
        raw = source.getvalue()
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)
        return list(csv.DictReader(io.StringIO(text)))
    if hasattr(source, "read"):
        raw = source.read()
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)
        return list(csv.DictReader(io.StringIO(text)))
    with open(source, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def import_reference_rate_csv(conn, path_or_file):
    """benchmark_code,rate_date,rate_value,source[,published_at] 형식."""
    rows = _read_csv_rows(path_or_file)
    required = {"benchmark_code", "rate_date", "rate_value", "source"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"기준금리 CSV 필수 열: {sorted(required)}")
    try:
        for row in rows:
            code = row["benchmark_code"].strip().upper()
            date = row["rate_date"].strip().replace("-", "")
            datetime.strptime(date, "%Y%m%d")
            value = float(row["rate_value"])
            if code in {"CD", "1CD"} or not code or not math.isfinite(value) or not row["source"].strip():
                raise ValueError(f"기준금리 코드·값·출처를 확인하세요: {date}")
            upsert_reference_rate(conn, code, date, value, row["source"].strip(), row.get("published_at"))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def import_frn_terms_csv(conn, path_or_file):
    """검증된 가산금리 또는 역산용 조건 CSV를 적재한다."""
    rows = _read_csv_rows(path_or_file)
    required = {"isin_code", "benchmark_code", "confidence_status"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"FRN 조건 CSV 필수 열: {sorted(required)}")
    numeric = {"spread_bps": float, "reference_rate": float,
               "coupon_rate_at_issue": float, "fixing_lag_bdays": int}
    try:
        for row in rows:
            isin = row["isin_code"].strip()
            code = row["benchmark_code"].strip().upper()
            status = row["confidence_status"].strip().upper()
            if not isin or not code or code in {"CD", "1CD"}:
                raise ValueError(f"{isin}: 기준금리는 CD91, CD1M처럼 기간을 명시해야 합니다.")
            values = {}
            for key, caster in numeric.items():
                raw = (row.get(key) or "").strip()
                if key == "fixing_lag_bdays" and raw and not float(raw).is_integer():
                    raise ValueError(f"{isin}: fixing_lag_bdays는 정수여야 합니다.")
                values[key] = caster(float(raw)) if raw else None
                if values[key] is not None and not math.isfinite(values[key]):
                    raise ValueError(f"{isin}: {key}가 유한한 숫자가 아닙니다.")
            spread = values["spread_bps"]
            if spread is None and values["coupon_rate_at_issue"] is not None and values["reference_rate"] is not None:
                spread = round((values["coupon_rate_at_issue"] - values["reference_rate"]) * 100, 4)
            fixing_date = (row.get("fixing_date") or "").strip().replace("-", "") or None
            if fixing_date:
                datetime.strptime(fixing_date, "%Y%m%d")
            source_ref = (row.get("source_ref") or "").strip()
            if status == "CONFIRMED" and (spread is None or not source_ref):
                raise ValueError(f"{isin}: CONFIRMED에는 spread_bps와 발행조건 원문 source_ref가 필요합니다.")
            if status == "INFERRED":
                required_values = [fixing_date, values["reference_rate"], values["coupon_rate_at_issue"],
                                   values["fixing_lag_bdays"], source_ref]
                if any(v is None or v == "" for v in required_values):
                    raise ValueError(f"{isin}: INFERRED에는 fixing_date, reference_rate, coupon_rate_at_issue, fixing_lag_bdays, source_ref가 필요합니다.")
                if values["fixing_lag_bdays"] < 0:
                    raise ValueError(f"{isin}: fixing_lag_bdays는 0 이상이어야 합니다.")
                rate_row = conn.execute(
                    "SELECT rate_value FROM reference_rate_daily WHERE benchmark_code=? AND rate_date=?",
                    (code, fixing_date),
                ).fetchone()
                if rate_row is None or abs(rate_row[0] - values["reference_rate"]) > 0.0001:
                    raise ValueError(f"{isin}: fixing_date의 {code} 원자료와 reference_rate가 일치하지 않습니다.")
                calculated = round((values["coupon_rate_at_issue"] - values["reference_rate"]) * 100, 4)
                if spread is not None and abs(calculated - spread) > 0.5:
                    status = "CONFLICT"
            upsert_frn_term(
                conn, isin, code, spread_bps=spread, fixing_date=fixing_date,
                reference_rate=values["reference_rate"], coupon_rate_at_issue=values["coupon_rate_at_issue"],
                reset_frequency=(row.get("reset_frequency") or "").strip() or None,
                fixing_lag_bdays=values["fixing_lag_bdays"], confidence_status=status,
                source_type=(row.get("source_type") or "").strip() or None,
                source_ref=source_ref or None, notes=(row.get("notes") or "").strip() or None,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="FRN 기준금리/조건 적재")
    sub = parser.add_subparsers(dest="command", required=True)
    fetch = sub.add_parser("fetch-ecos-cd91")
    fetch.add_argument("start_date", help="YYYYMMDD")
    fetch.add_argument("end_date", help="YYYYMMDD")
    imp_rate = sub.add_parser("import-rates")
    imp_rate.add_argument("csv_path")
    imp_terms = sub.add_parser("import-terms")
    imp_terms.add_argument("csv_path")
    args = parser.parse_args()

    init_db()
    conn = get_connection()
    try:
        if args.command == "fetch-ecos-cd91":
            key = load_ecos_api_key(required=True)
            rows = fetch_ecos_cd91(key, args.start_date, args.end_date)
            count = save_reference_rates(conn, rows)
        elif args.command == "import-rates":
            count = import_reference_rate_csv(conn, args.csv_path)
        else:
            count = import_frn_terms_csv(conn, args.csv_path)
        print(f"완료: {count:,}건")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
