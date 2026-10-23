"""
종목명(korSecnNm / isinCdNm) 기반 섹터 자동 분류 모듈 (주요 발행 주체 세분화 적용)
- 금융위 API 전수 데이터 정밀 분류
- 국고채/공공채(GOV)를 시장 주요 발행 주체별로 세분화:
  1. GOV_TREASURY: 국고채, 국민주택채, 지방채
  2. GOV_HOUSING: 한국주택금융공사(MBS), LH한국토지주택공사, SH, GH, HUG
  3. GOV_KEPCO: 한국전력공사(한전채) 및 발전 5사(서부/중부/남동/남부/동서발전)
  4. GOV_INFRA: 한국도로공사, 한국가스공사, 한국철도공사, 국가철도공단, 한국수자원공사, 항만공사
  5. GOV_POLICY: 중소벤처기업진흥공단, 한국자산관리공사(캠코), 신용보증기금, 기타 공공기관
"""
import re

SECTOR_RULES = [
    # 1. 국고채 / 국민주택채 / 지방채 (정부 직발행/지자체)
    {
        "code": "GOV_TREASURY",
        "label": "국고/정부채",
        "patterns": [
            r"^국고", r"^국민주택", r"지방채", r"예금보험", r"재정증권"
        ]
    },
    # 2. 한국주택금융공사(MBS) 및 LH / 주택 관련 공공기관
    {
        "code": "GOV_HOUSING",
        "label": "주택금융/LH채",
        "patterns": [
            r"한국주택금융", r"주택금융공사", r"토지주택공사", r"^LH", r"주택도시보증", r"HUG",
            r"^SH", r"^GH", r"경기주택도시"
        ]
    },
    # 3. 한국전력공사(한전채) 및 발전 5사
    {
        "code": "GOV_KEPCO",
        "label": "한전/발전사채",
        "patterns": [
            r"한국전력", r"한전", r".*발전"
        ]
    },
    # 4. 도로, 가스, 철도, 수자원, 항만, 교통, 공항 등 인프라/SOC 공사채
    {
        "code": "GOV_INFRA",
        "label": "도로/가스/인프라채",
        "patterns": [
            r"도로공사", r"가스공사", r"철도공사", r"철도공단", r"수자원", r"항만공사",
            r"교통공사", r"공항공사"
        ]
    },
    # 5. 중진공, 캠코, 신보, 증권금융 등 진흥/정책 공공기관채
    {
        "code": "GOV_POLICY",
        "label": "기타 공공기관채",
        "patterns": [
            r"자산관리", r"중소벤처기업진흥", r"증권금융", r"보증기금", r"도시공사", r".*공단"
        ]
    },
    # 6. 신용카드사 (CARD)
    {
        "code": "CARD",
        "label": "신용카드채",
        "patterns": [
            r"신한카드", r"삼성카드", r"현대카드", r"국민카드", r"KB국민카드",
            r"롯데카드", r"하나카드", r"우리카드", r"비씨카드", r"BC카드"
        ]
    },
    # 7. 캐피탈 / 리스 / 파이낸셜 (CAPITAL)
    {
        "code": "CAPITAL",
        "label": "캐피탈/여전채",
        "patterns": [
            r".*캐피탈", r".*오토리스", r".*파이낸셜", r"현대커머셜",
            r"애큐온", r"오케이파이낸셜", r"메리츠캐피탈", r"아이엠캐피탈", r"IBK캐피탈"
        ]
    },
    # 8. 4대 시중은행, 금융지주, 지방은행 및 증권사 (BANK)
    {
        "code": "BANK",
        "label": "시중은행/증권채",
        "patterns": [
            r"국민은행", r"신한은행", r"하나은행", r"우리은행",
            r"^국민\d", r"^신한\d", r"^하나\d", r"^우리\d",
            r"대구은행", r"부산은행", r"경남은행", r"전북은행", r"광주은행", r"제주은행",
            r"iM뱅크", r"SC제일은행", r"스탠다드차타드", r"씨티은행",
            r"KB금융지주", r"신한금융지주", r"하나금융지주", r"우리금융지주", r"농협금융지주",
            r".*증권"
        ]
    },
    # 9. 특수/국책은행 및 전용 금융채 (SBANK)
    {
        "code": "SBANK",
        "label": "특수/국책은행채",
        "patterns": [
            r"기업은행", r"산업은행", r"농협은행", r"수협은행", r"수출입은행",
            r"중소기업은행", r"한국수출입은행",
            r"산업금융채권", r"산금채", r"산은", r"한국수출입금융", r"수은", r"농업금융채권", r"농재채",
            r"수산금융채권", r"중소기업금융", r"중기채"
        ]
    },
]


def classify_sector(bond_name):
    """채권 종목명을 입력받아 섹터 코드와 한글 라벨을 반환합니다."""
    if not bond_name:
        return "OTHER", "기타 회사채"

    for rule in SECTOR_RULES:
        for pattern in rule["patterns"]:
            if re.search(pattern, bond_name, re.IGNORECASE):
                return rule["code"], rule["label"]

    return "OTHER", "기타 회사채"


def extract_issuer_name(bond_name):
    """종목명에서 추정 기관명을 추출합니다."""
    if not bond_name:
        return "미상"

    keywords = [
        "KB국민은행", "국민은행", "신한은행", "하나은행", "우리은행", "기업은행", "산업은행", "농협은행",
        "한국주택금융공사", "한국전력공사", "한국토지주택공사", "한국도로공사", "한국가스공사", "한국철도공사",
        "신한카드", "삼성카드", "현대카드", "KB국민카드", "롯데카드", "하나카드", "우리카드",
        "현대캐피탈", "KB캐피탈", "신한캐피탈", "하나캐피탈", "우리금융캐피탈", "롯데캐피탈", "아주캐피탈",
        "국고", "국민주택", "산업금융", "수출입금융", "농업금융"
    ]
    for kw in keywords:
        if kw in bond_name:
            return kw

    match = re.match(r"^([가-힣a-zA-Z\s]+)", bond_name)
    if match:
        return match.group(1).strip()

    return bond_name[:10]
