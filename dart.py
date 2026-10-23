"""OpenDART 은행 채무증권 공시의 FRN 조건 *후보*를 조회한다.

공시 행은 ISIN과 자동 연결하지 않는다. 원문 및 채권 종목을 대조한 뒤
frn_terms CSV의 CONFIRMED 행으로 옮기는 검토용 도구다.
"""
import argparse
import csv
import html
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

from config import load_env_values

DART_API = "https://opendart.fss.or.kr/api"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="


def _request(endpoint, api_key, **params):
    query = urllib.parse.urlencode({"crtfc_key": api_key, **params})
    try:
        with urllib.request.urlopen(f"{DART_API}/{endpoint}?{query}", timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OpenDART HTTP {exc.code} ({endpoint})") from None
    except urllib.error.URLError:
        raise RuntimeError(f"OpenDART 연결 실패 ({endpoint})") from None


def _json(endpoint, api_key, **params):
    payload = json.loads(_request(endpoint, api_key, **params).decode("utf-8"))
    status = payload.get("status")
    if status not in {"000", "013"}:
        raise RuntimeError(f"OpenDART {endpoint}: [{status}] {payload.get('message', '')}")
    return payload


def _zip_members(blob, limit_bytes=40_000_000):
    if not zipfile.is_zipfile(io.BytesIO(blob)):
        try:
            root = ET.fromstring(blob)
            raise RuntimeError(f"OpenDART 파일 오류: [{root.findtext('status')}] {root.findtext('message')}")
        except ET.ParseError:
            raise RuntimeError("OpenDART ZIP 응답이 아닙니다.") from None
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        members = [m for m in archive.infolist() if m.filename.lower().endswith(".xml")]
        if sum(m.file_size for m in members) > limit_bytes:
            raise RuntimeError("OpenDART 압축 해제 크기 제한을 초과했습니다.")
        return [(m.filename, archive.read(m)) for m in members]


def find_corp_codes(api_key, issuer_name):
    """DART 고유번호는 법인등록번호와 다르다. 정식 회사명 정확 일치만 허용한다."""
    target = issuer_name.replace("주식회사", "").replace("㈜", "").replace(" ", "")
    result = []
    for _, content in _zip_members(_request("corpCode.xml", api_key)):
        root = ET.fromstring(content)
        for item in root.findall(".//list"):
            name = (item.findtext("corp_name") or "").strip()
            normalized = name.replace("주식회사", "").replace("㈜", "").replace(" ", "")
            if normalized == target:
                result.append({"corp_code": item.findtext("corp_code"), "corp_name": name})
    return result


def _flatten_groups(payload):
    groups = payload.get("group", [])
    if isinstance(groups, dict):
        groups = [groups]
    for group in groups:
        rows = group.get("list", [])
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            if "bdnmn" in row or "intr" in row:
                yield row


def fetch_debt_security_candidates(api_key, corp_code, start_date, end_date):
    """증권신고서 주요정보 bdRs의 이자율 문자열을 후보로 수집한다."""
    if len(corp_code) != 8 or not corp_code.isdigit():
        raise ValueError("DART corp_code는 8자리 숫자여야 합니다.")
    if datetime.strptime(start_date, "%Y%m%d") > datetime.strptime(end_date, "%Y%m%d"):
        raise ValueError("시작일이 종료일보다 늦습니다.")
    payload = _json("bdRs.json", api_key, corp_code=corp_code, bgn_de=start_date, end_de=end_date)
    if payload.get("status") == "013":
        return []
    seen, result = set(), []
    for row in _flatten_groups(payload):
        key = (row.get("rcept_no"), row.get("tm"), row.get("bdnmn"))
        if key in seen:
            continue
        seen.add(key)
        combined = " ".join(str(row.get(k) or "") for k in ("bdnmn", "intr"))
        if not re.search(r"FRN|변동|KOFR|CD\s*(?:91|\+)|1\s*CD", combined, re.I):
            continue
        receipt = str(row.get("rcept_no") or "")
        result.append({
            "corp_name": row.get("corp_name", ""), "corp_code": corp_code,
            "rcept_no": receipt, "source_ref": DART_VIEW + receipt if receipt else "",
            "issue_date": row.get("pymd", ""), "maturity_date": row.get("rpd", ""),
            "series": row.get("tm", ""), "bond_name": row.get("bdnmn", ""),
            "interest_text": row.get("intr", ""), "face_amount": row.get("fta", ""),
            "review_status": "CANDIDATE_ISIN_AND_TERMS_REVIEW_REQUIRED",
        })
    return result


def fetch_document_snippets(api_key, rcept_no, context=160):
    """원문 XML의 FRN 관련 텍스트 주변만 검토용으로 반환한다."""
    if len(rcept_no) != 14 or not rcept_no.isdigit():
        raise ValueError("rcept_no는 14자리 숫자여야 합니다.")
    snippets = []
    for filename, content in _zip_members(_request("document.xml", api_key, rcept_no=rcept_no)):
        decoded = content.decode("utf-8", errors="replace")
        if decoded.count("\ufffd") > 50:
            decoded = content.decode("cp949", errors="replace")
        plain = html.unescape(re.sub(r"<[^>]+>", " ", decoded))
        plain = re.sub(r"\s+", " ", plain)
        for match in re.finditer(r"FRN|변동금리|가산금리|KOFR|1\s*CD|CD\s*91", plain, re.I):
            snippets.append({"filename": filename, "text": plain[max(0, match.start()-context):match.end()+context]})
            if len(snippets) >= 100:
                return snippets
    return snippets


def main():
    parser = argparse.ArgumentParser(description="OpenDART 은행 FRN 공시 후보 조회")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("candidates")
    search.add_argument("--issuer", help="정식 회사명 (DART 고유번호 정확 일치 조회)")
    search.add_argument("--corp-code", help="8자리 DART 고유번호. 같은 회사명이 여럿일 때 지정")
    search.add_argument("--start", required=True, help="YYYYMMDD")
    search.add_argument("--end", required=True, help="YYYYMMDD")
    search.add_argument("--out", default="data/dart_candidates.csv")
    doc = sub.add_parser("snippets")
    doc.add_argument("rcept_no")
    doc.add_argument("--out", default="data/dart_snippets.json")
    args = parser.parse_args()
    key = load_env_values().get("DART_API_KEY")
    if not key:
        raise ValueError(".env에 DART_API_KEY를 설정해 주세요.")
    if args.command == "candidates":
        if not args.corp_code and not args.issuer:
            parser.error("--issuer 또는 --corp-code가 필요합니다.")
        corp_code = args.corp_code
        if not corp_code:
            matches = find_corp_codes(key, args.issuer)
            if len(matches) != 1:
                raise ValueError(f"DART 회사명 일치 {len(matches)}건: {matches}. --corp-code로 지정하세요.")
            corp_code = matches[0]["corp_code"]
        rows = fetch_debt_security_candidates(key, corp_code, args.start, args.end)
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        columns = ["corp_name", "corp_code", "rcept_no", "source_ref", "issue_date",
                   "maturity_date", "series", "bond_name", "interest_text", "face_amount", "review_status"]
        with output.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        print(f"공시 후보 {len(rows)}건: {output}")
    else:
        snippets = fetch_document_snippets(key, args.rcept_no)
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"rcept_no": args.rcept_no, "source_ref": DART_VIEW + args.rcept_no,
                                      "snippets": snippets}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"원문 스니펫 {len(snippets)}건: {output}")


if __name__ == "__main__":
    main()
