"""브로커 메시지나 사용자 입력에서 FRN 발행조건을 추출한다."""
import re

ISSUER_ALIASES = {
    "한국산업은행": "산업은행", "산업은행": "산업은행", "산금채": "산업은행",
    "한국수출입은행": "수출입은행", "수출입은행": "수출입은행", "수출입금융채권": "수출입은행",
    "중소기업은행": "기업은행", "IBK기업은행": "기업은행", "기업은행": "기업은행", "중금채": "기업은행",
    "NH농협은행": "농협은행", "농협은행": "농협은행", "농금채": "농협은행",
    "수협은행": "수협은행",
    "KB국민은행": "국민은행", "국민은행": "국민은행", "국민채": "국민은행",
    "신한은행": "신한은행", "신한채": "신한은행",
    "하나은행": "하나은행", "하나채": "하나은행",
    "우리은행": "우리은행", "우리채": "우리은행",
    "SC제일은행": "SC제일은행", "한국스탠다드차타드은행": "SC제일은행",
    "한국씨티은행": "한국씨티은행",
    "부산은행": "부산은행", "대구은행": "iM뱅크", "iM뱅크": "iM뱅크",
    "경남은행": "경남은행", "광주은행": "광주은행", "전북은행": "전북은행",
    "제주은행": "제주은행", "카카오뱅크": "카카오뱅크",
    "케이뱅크": "케이뱅크", "토스뱅크": "토스뱅크",
}

TENOR_MONTHS_TO_LABEL = {
    6: "6개월", 9: "9개월", 12: "1년", 15: "1.25년", 18: "1.5년",
    21: "1.75년", 24: "2년", 36: "3년", 60: "5년", 120: "10년",
}


def _normalize_tenor(number, unit):
    months = float(number) * (12 if unit == "년" else 1)
    target = min(TENOR_MONTHS_TO_LABEL, key=lambda x: abs(x - months))
    return TENOR_MONTHS_TO_LABEL[target], months


def parse_frn_quote(text):
    """입력 예: '하나채 1.5년 FRN 1CD+59bp'."""
    raw = (text or "").strip()
    compact = re.sub(r"\s+", "", raw)
    issuer = None
    for alias in sorted(ISSUER_ALIASES, key=len, reverse=True):
        if alias.lower() in compact.lower():
            issuer = ISSUER_ALIASES[alias]
            break

    tenor_match = re.search(r"(\d+(?:\.\d+)?)\s*(년|개월|M|Y)", raw, re.I)
    maturity_bucket = None
    tenor_months = None
    if tenor_match:
        unit = tenor_match.group(2).upper()
        unit = "개월" if unit == "M" else "년" if unit == "Y" else tenor_match.group(2)
        maturity_bucket, tenor_months = _normalize_tenor(tenor_match.group(1), unit)

    benchmark_match = re.search(r"(KOFR|CD\s*91|\d+\s*CD|CD)", raw, re.I)
    benchmark_raw = re.sub(r"\s+", "", benchmark_match.group(1)).upper() if benchmark_match else None
    if benchmark_raw == "CD91":
        benchmark_code = "CD91"
        benchmark_warning = None
    elif benchmark_raw == "1CD":
        benchmark_code = "CD1M"
        benchmark_warning = "1CD는 CD 1개월물 후보로 분류했습니다. 실제 채권의 기준금리·고시 방식은 발행조건 원문에서 확인하세요. CD91과 합산하지 않습니다."
    elif benchmark_raw == "CD":
        benchmark_code = None
        benchmark_warning = "CD의 기간이 명시되지 않았습니다. CD91 또는 1CD 등 발행조건 원문을 확인하세요."
    elif benchmark_raw == "KOFR":
        benchmark_code = "KOFR"
        benchmark_warning = None
    elif benchmark_raw:
        benchmark_code = benchmark_raw
        benchmark_warning = f"{benchmark_raw}의 공식 기준금리 매핑을 확인해야 합니다."
    else:
        benchmark_code = None
        benchmark_warning = "기준금리를 인식하지 못했습니다."

    spread_match = re.search(r"(?:KOFR|CD\s*91|\d+\s*CD|CD)\s*\+\s*(\d+(?:\.\d+)?)\s*(?:BP|BPS)?", raw, re.I)
    spread_bps = float(spread_match.group(1)) if spread_match else None
    return {
        "raw_text": raw,
        "issuer": issuer,
        "maturity_bucket": maturity_bucket,
        "tenor_months_input": tenor_months,
        "benchmark_raw": benchmark_raw,
        "benchmark_code": benchmark_code,
        "spread_bps": spread_bps,
        "benchmark_warning": benchmark_warning,
        "is_complete": all(x is not None for x in (issuer, maturity_bucket, benchmark_code, spread_bps)),
    }
