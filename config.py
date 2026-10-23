"""
전역 설정값 관리 모듈
- API 엔드포인트, DB 경로, 발행사 매핑 등
"""
import os

# ──────────────────────────────────────────────
# API 설정
# ──────────────────────────────────────────────
BASE_URL = "https://apis.data.go.kr/B552481/BondSvc"

def load_env_values(env_path=None):
    """Public.env와 .env를 읽되, .env에 입력한 값이 우선한다."""
    paths = [env_path] if env_path is not None else [
        os.path.join(os.path.dirname(__file__), "Public.env"),
        os.path.join(os.path.dirname(__file__), ".env"),
    ]
    values = {}
    found = False
    for path in paths:
        if not os.path.exists(path):
            continue
        found = True
        with open(path, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    if value.strip():
                        values[key.strip()] = value.strip()
                elif "API_KEY" not in values:
                    values["API_KEY"] = line
    if not found:
        raise FileNotFoundError(".env 또는 Public.env 파일이 없습니다.")
    return values


def load_api_key(env_path=None):
    """공공데이터포털 API 키를 로드한다."""
    value = load_env_values(env_path).get("API_KEY", "")
    if not value:
        raise ValueError(".env에 API_KEY를 입력해 주세요.")
    return value


def load_ecos_api_key(env_path=None, required=False):
    """한국은행 ECOS 키를 로드한다. 선택 설정이므로 기본값은 None이다."""
    value = load_env_values(env_path).get("ECOS_API_KEY", "")
    if required and not value:
        raise ValueError(".env에 ECOS_API_KEY를 추가해 주세요.")
    return value or None

# ──────────────────────────────────────────────
# DB 설정
# ──────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "bond_data.db")

# ──────────────────────────────────────────────
# 발행사 매핑 (issucoCustno → 섹터)
# issucoCustno는 예탁원 기업정보서비스에서 부여하는 고객번호.
# 아래는 대표적인 4대 시중은행 + 주요 여전사를 수동 매핑한 것.
# 실제 고객번호는 기업정보서비스 API 또는 세이브로(SEIBro)에서 확인 필요.
# ──────────────────────────────────────────────
ISSUER_MAPPING = {
    # ── 시중은행 (BANK) ──
    "00149985": {"name": "KB국민은행",     "sector": "BANK",    "rating": "AAA"},
    "00149979": {"name": "신한은행",       "sector": "BANK",    "rating": "AAA"},
    "00149980": {"name": "하나은행",       "sector": "BANK",    "rating": "AAA"},
    "00149981": {"name": "우리은행",       "sector": "BANK",    "rating": "AAA"},
    # ── 특수은행 (SBANK) ──
    "00149987": {"name": "한국산업은행",   "sector": "SBANK",   "rating": "AAA"},
    "00206422": {"name": "IBK기업은행",    "sector": "SBANK",   "rating": "AAA"},
    "00149990": {"name": "NH농협은행",     "sector": "SBANK",   "rating": "AAA"},
    # ── 신용카드 (CARD) ──
    "00252876": {"name": "삼성카드",       "sector": "CARD",    "rating": "AA+"},
    "00252877": {"name": "현대카드",       "sector": "CARD",    "rating": "AA+"},
    "00252873": {"name": "신한카드",       "sector": "CARD",    "rating": "AA+"},
    "00252874": {"name": "KB국민카드",     "sector": "CARD",    "rating": "AA+"},
    "00252875": {"name": "하나카드",       "sector": "CARD",    "rating": "AA"},
    "00252878": {"name": "우리카드",       "sector": "CARD",    "rating": "AA"},
    # ── 캐피탈 (CAPITAL) ──
    "00253112": {"name": "현대캐피탈",     "sector": "CAPITAL", "rating": "AA+"},
    "00253113": {"name": "KB캐피탈",       "sector": "CAPITAL", "rating": "AA"},
    "00253114": {"name": "신한캐피탈",     "sector": "CAPITAL", "rating": "AA-"},
    "00253115": {"name": "하나캐피탈",     "sector": "CAPITAL", "rating": "AA-"},
    # ── 정부/공공 (GOV) ──
    "00000001": {"name": "대한민국(국고채)", "sector": "GOV",   "rating": "AAA"},
}

# 섹터 코드 → 한글 라벨 (주요 발행 주체 세분화)
SECTOR_LABELS = {
    "GOV_TREASURY": "국고/정부채",
    "GOV_HOUSING":  "주택금융/LH채",
    "GOV_KEPCO":    "한전/발전사채",
    "GOV_INFRA":    "도로/가스/인프라채",
    "GOV_POLICY":   "기타 공공기관채",
    "BANK":         "시중은행/증권채",
    "SBANK":        "특수/국책은행채",
    "CARD":         "신용카드채",
    "CAPITAL":      "캐피탈/여전채",
    "OTHER":        "기타 회사채",
}
