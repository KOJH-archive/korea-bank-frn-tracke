"""은행 FRN 발행 시계열 분석. 원만기 기준이며 API 원자료만 사용한다."""
from datetime import datetime
import pandas as pd

CORE_ISSUERS = {
    "정책·특수은행": ["한국산업은행", "한국수출입은행", "중소기업은행", "IBK기업은행", "농협은행", "수협은행"],
    "시중은행": ["국민은행", "KB국민은행", "신한은행", "하나은행", "우리은행", "SC제일은행", "한국스탠다드차타드은행", "한국씨티은행"],
    "지방·인터넷은행": ["부산은행", "대구은행", "iM뱅크", "경남은행", "광주은행", "전북은행", "제주은행", "카카오뱅크", "케이뱅크", "토스뱅크"],
}

ALIASES = {
    "한국산업은행": "산업은행", "한국수출입은행": "수출입은행",
    "중소기업은행": "기업은행", "IBK기업은행": "기업은행",
    "KB국민은행": "국민은행", "농협은행": "농협은행",
    "한국스탠다드차타드은행": "SC제일은행",
    "대구은행": "iM뱅크",
}

ISSUER_GROUP_BY_CANONICAL = {
    ALIASES.get(name, name): group_name
    for group_name, names in CORE_ISSUERS.items()
    for name in names
}

# 은행채 시장에서 자주 쓰는 표준 발행만기. 경계는 인접 표준만기의 중간값이다.
# 예: 17개월은 1.5년, 20개월은 1.75년으로 분류된다.
MATURITY_TARGET_MONTHS = [6, 9, 12, 15, 18, 21, 24, 36, 60, 120]
MATURITY_LABELS = ["6개월", "9개월", "1년", "1.25년", "1.5년", "1.75년", "2년", "3년", "5년", "10년"]
MATURITY_BINS_MONTHS = [0, 7.5, 10.5, 13.5, 16.5, 19.5, 22.5, 30, 48, 90, float("inf")]

COMMERCIAL_BANKS = ["국민은행", "신한은행", "하나은행", "우리은행", "농협은행"]

FRN_STRUCTURE_LABELS = {
    "STRAIGHT_FRN": "직선형 FRN",
    "FIXED_TO_FLOAT": "고정→변동 전환",
    "OTHER_FLOATING": "기타·유형 미확인",
}

def canonical_issuer(value):
    value = (value or "").replace("주식회사", "").replace("㈜", "").replace(" ", "")
    for group in CORE_ISSUERS.values():
        for name in group:
            if name.replace(" ", "") in value:
                return ALIASES.get(name, name)
    return None


def add_frn_structure(df):
    """공식 금리유형을 우선해 FRN 구조를 분리한다.

    금융위 코드 2는 발행일부터 변동, 코드 3은 고정+변동이다. 공식 코드가
    없더라도 원문 발행조건이 확보된 종목은 직선형 FRN으로 간주하고,
    종목명만으로 탐지된 나머지는 시장 규모에서 자동 포함하지 않는다.
    """
    if df.empty:
        result = df.copy()
        result["frn_structure"] = pd.Series(dtype="object")
        result["frn_structure_label"] = pd.Series(dtype="object")
        return result
    result = df.copy()
    rate_code = result.get("rate_type_code", pd.Series(index=result.index, dtype="object")).astype(str)
    rate_name = result.get("rate_type_name", pd.Series(index=result.index, dtype="object")).fillna("").astype(str)
    confirmed_terms = result.get("benchmark_code", pd.Series(index=result.index, dtype="object")).notna()
    result["frn_structure"] = "OTHER_FLOATING"
    result.loc[(rate_code == "2") | confirmed_terms, "frn_structure"] = "STRAIGHT_FRN"
    result.loc[(rate_code == "3") | rate_name.str.contains("고정\\s*\\+\\s*변동", regex=True), "frn_structure"] = "FIXED_TO_FLOAT"
    result["frn_structure_label"] = result["frn_structure"].map(FRN_STRUCTURE_LABELS)
    return result

def load_bank_frn(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bond_master)")}
    optional = ["coupon_rate", "rate_type_code", "rate_type_name", "interest_type_code", "interest_type_name"]
    select_optional = ",".join(c if c in cols else f"NULL AS {c}" for c in optional)
    terms_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='frn_terms'"
    ).fetchone() is not None
    terms_cols = ""
    terms_join = ""
    if terms_exists:
        terms_cols = (
            ",t.benchmark_code,t.spread_bps,t.fixing_date,"
            "t.reference_rate,t.coupon_rate_at_issue,t.confidence_status,"
            "t.source_type,t.source_ref"
        )
        terms_join = "LEFT JOIN frn_terms t ON t.isin_code=b.isin_code"
    rows = conn.execute(f"""
        SELECT b.isin_code,b.bond_name,b.issue_date,b.maturity_date,b.issue_amount,
               i.issuer_name,{select_optional}{terms_cols}
        FROM bond_master b LEFT JOIN issuer_mapping i ON i.issuer_id=b.issuer_id
        {terms_join}
        WHERE b.currency='KRW' AND length(b.issue_date)=8 AND length(b.maturity_date)=8
    """).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["issuer"] = df["issuer_name"].map(canonical_issuer)
    df = df[df["issuer"].notna()].copy()
    df["issuer_group"] = df["issuer"].map(ISSUER_GROUP_BY_CANONICAL).fillna("기타")
    df["issue_dt"] = pd.to_datetime(df["issue_date"], format="%Y%m%d", errors="coerce")
    df["maturity_dt"] = pd.to_datetime(df["maturity_date"], format="%Y%m%d", errors="coerce")
    df["original_tenor_months"] = (df["maturity_dt"]-df["issue_dt"]).dt.days / (365.25 / 12)
    df["original_tenor_years"] = df["original_tenor_months"] / 12
    df["maturity_bucket"] = pd.cut(
        df["original_tenor_months"], MATURITY_BINS_MONTHS,
        labels=MATURITY_LABELS, right=False, ordered=True
    )
    df["issue_month"] = df["issue_dt"].dt.to_period("M").astype(str)
    df["issue_amount_억원"] = pd.to_numeric(df["issue_amount"], errors="coerce") / 1e8
    # API 명시값을 우선하며 코드/종목명은 보조 판정이다.
    text = df["rate_type_name"].fillna("").astype(str) + " " + df["bond_name"].fillna("").astype(str)
    name_candidate = text.str.contains(
        r"변동|(?:\(\s*변\s*\)|[-\s]변(?:[-\s0-9)]|$))|FRN|FLT|CD\+|KOFR",
        case=False, regex=True
    )
    official_candidate = df["rate_type_code"].astype(str).isin(["2", "3"])
    df["is_frn"] = name_candidate | official_candidate
    # 금융위 API의 공식 금리변동 코드가 있으면 종목명 표기와 관계없이 후보로 포함한다.
    if "benchmark_code" in df.columns:
        df["is_frn"] = df["is_frn"] | df["benchmark_code"].notna()
    # 후보의 근거를 화면에 노출해 '검증 조건 0건'과 혼동하지 않도록 한다.
    df["frn_detection"] = "종목명 후보"
    df.loc[official_candidate, "frn_detection"] = "금융위 변동금리 코드"
    df.loc[official_candidate & name_candidate, "frn_detection"] = "금융위 코드 + 종목명"
    if "benchmark_code" in df.columns:
        df.loc[df["benchmark_code"].notna() & ~official_candidate, "frn_detection"] = "적재된 FRN 조건"
    df["benchmark_hint"] = "미확인"
    df.loc[text.str.contains("KOFR", case=False), "benchmark_hint"] = "KOFR"
    df.loc[text.str.contains("CD", case=False), "benchmark_hint"] = "CD"
    df.loc[text.str.contains(r"1\s*CD", case=False, regex=True), "benchmark_hint"] = "CD1M 후보"
    if "benchmark_code" in df.columns:
        df["benchmark_hint"] = df["benchmark_code"].fillna(df["benchmark_hint"])
    return add_frn_structure(df[df["is_frn"]].copy())


def spread_percentile(series, value):
    """동률에 중간순위를 적용한 백분위. 검증된 숫자형 표본만 입력한다."""
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return None
    below = (clean < value).sum()
    equal = (clean == value).sum()
    return 100.0 * (below + 0.5 * equal) / len(clean)


def comparable_spreads(df, issuer, maturity_bucket, benchmark_code,
                       allowed_status=("CONFIRMED", "INFERRED")):
    """동일 발행사·표준 원만기·기준금리의 검증 표본을 반환한다."""
    required = {"spread_bps", "confidence_status", "benchmark_code"}
    if df.empty or not required.issubset(df.columns):
        return df.iloc[0:0].copy()
    return df[
        (df["issuer"] == issuer)
        & (df["maturity_bucket"].astype(str) == maturity_bucket)
        & (df["benchmark_code"] == benchmark_code)
        & (df["confidence_status"].isin(allowed_status))
        & (pd.to_numeric(df["spread_bps"], errors="coerce").notna())
    ].copy()
