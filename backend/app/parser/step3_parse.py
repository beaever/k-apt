"""3단계: 첨부 공고문(PDF/HWP) 텍스트 파싱 -> 자본금/실적/공법번호 추출.

값을 못 찾았을 때는 빈 문자열이 아니라 "왜 없는지"를 그 자리에 채운다
(첨부파일 자체가 없는지 / 다운로드가 실패했는지 / 텍스트 추출이 안 됐는지 / 문서에 해당 문구가 없는지).
"""

import os
import re
import subprocess
import tempfile

import pdfplumber
from bs4 import BeautifulSoup

FIELDS = ["capital", "record", "patent_no"]

# 공고문 표 헤더에 "자 본 금", "실 적"처럼 글자 사이를 띄어 쓰는 관행이 매우 흔해
# (실제 631개 첨부파일 스캔 결과 자본금 미검출 사례의 대다수가 이 패턴이었음) 글자 사이
# 공백을 허용하는 정규식으로 키워드를 찾는다.
CAPITAL_KEYWORD = re.compile(r"자\s*본\s*금")
RECORD_KEYWORD = re.compile(r"실\s*적")
PATENT_KEYWORD = re.compile(r"특\s*허")
METHOD_KEYWORD = re.compile(r"공\s*법")
PATENT_NO = re.compile(r"특\s*허\s*제?\s*([\d\-]+)\s*호")
# "실적" 단어 없이 "최근 N년간 ... M건 이상"으로만 표현되는 공고문 대비 폴백 패턴
RECORD_FALLBACK = re.compile(r"최근\s*\d+\s*년간.*?\d+\s*건\s*이상")

DOWNLOAD_FAILED_REASON = "공고문을 확보하지 못해 확인 불가"
NOT_FOUND_REASON = "공고문 내용에서 해당 항목 관련 문구를 찾지 못함"


def extract_text(path: str) -> tuple[str, str | None]:
    """(추출된 텍스트, 실패 사유) 반환. 성공하면 실패 사유는 None."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        try:
            with pdfplumber.open(path) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception as e:
            return "", f"PDF를 열지 못함({e})"
        if not text.strip():
            return "", "PDF에서 텍스트를 추출하지 못함(스캔 이미지 PDF일 가능성)"
        return text, None

    if ext == ".hwp":
        # hwp5txt는 표(table) 안의 텍스트를 "<표>" placeholder로만 남기고 누락시킨다.
        # 참가자격/실적 요건이 표로 작성된 공고문이 많아, hwp5html로 변환 후 HTML을 파싱해
        # 표 내용까지 포함한 전체 텍스트를 얻는다.
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["hwp5html", "--output", tmpdir, path], capture_output=True, text=True
            )
            if result.returncode != 0:
                reason = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "알 수 없는 오류"
                return "", f"HWP 파싱 실패({reason[:100]})"
            index_path = os.path.join(tmpdir, "index.xhtml")
            if not os.path.exists(index_path):
                return "", "HWP에서 텍스트를 추출하지 못함(변환 결과 없음)"
            with open(index_path, encoding="utf-8") as f:
                text = BeautifulSoup(f.read(), "html.parser").get_text("\n")
        if not text.strip():
            return "", "HWP에서 텍스트를 추출하지 못함"
        return text, None

    # ponytail: .hwpx(OOXML)/.zip 등은 의뢰서 범위(HWP/PDF) 밖 -> 미지원, 필요해지면 zipfile+xml로 추가
    return "", f"지원하지 않는 첨부파일 형식({ext or '확장자 없음'})이라 텍스트 추출 불가"


# "1)", "2.", "가." 처럼 문단 앞에 붙는 번호 매김 표시. 이 안의 숫자는 "값"이 아니므로
# 다음 줄과 합칠지 판단할 때는 제외하고 봐야 한다.
LEADING_MARKER = re.compile(r"^[①-⑩]|^\d+[\)\.]\s*|^[가-힣][\.\)]\s*")


def _has_value_digit(line: str) -> bool:
    return bool(re.search(r"\d", LEADING_MARKER.sub("", line, count=1)))


# "자격제한요소 : 자본금, 사업실적, 기술능력)"처럼 항목 "이름"만 콤마로 나열한 제목 줄.
# 실제 요건 값은 보통 뒤쪽의 다른 번호 항목에 따로 나오므로, 이런 줄은 값으로 치지 않는다.
HEADING_LIST = re.compile(r"(?:자\s*본\s*금|실\s*적|특\s*허|공\s*법)\s*,")


def _find_line_with_value(lines: list[str], matches) -> str:
    """조건에 맞는 줄을 찾되, 값에 해당하는 숫자가 없으면 뒤 줄을 이어붙인다.

    표를 hwp5html로 변환하면 "자본금 :" 라벨과 "5억원 이상" 값이 서로 다른 줄(표 셀)로
    쪼개지는 문서가 있다 (실제 631개 파일 검증 중 발견). 숫자가 나올 때까지 최대 2줄을
    추가로 이어붙여 값이 잘리는 것을 막는다. (번호 매김 "1)"의 숫자는 값으로 치지 않음)
    항목 이름만 나열한 제목 줄(HEADING_LIST)은 후보에서 제외한다.
    """
    for i, line in enumerate(lines):
        if not matches(line) or HEADING_LIST.search(line):
            continue
        if _has_value_digit(line):
            return line
        merged = line
        for nxt in lines[i + 1 : i + 3]:
            merged = f"{merged} {nxt}"
            if _has_value_digit(nxt):
                break
        return merged
    return ""


def parse_requirements(text: str) -> dict:
    """공고문 텍스트에서 자본금/실적/공법번호 요건이 적힌 줄을 그대로 추출.

    수치를 엄격히 정규화하지 않고 요건 문장 전체를 값으로 사용 -> 표현이 제각각인
    공고문에서도 안정적으로 사람이 바로 읽을 수 있는 값을 얻는다.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    capital = _find_line_with_value(lines, CAPITAL_KEYWORD.search)
    record = _find_line_with_value(lines, RECORD_KEYWORD.search)
    if not record:
        record = next((line for line in lines if RECORD_FALLBACK.search(line)), "")
    # 특허/공법 요건은 숫자(특허번호) 없이 완결된 문장인 경우가 많아(예: "...공법 시공 가능
    # 업체") capital/record처럼 "숫자가 나올 때까지 이어붙이기"를 적용하면 오히려 관계없는
    # 다음 줄까지 잘못 붙는다. 첫 매칭 줄을 그대로 쓴다.
    patent_line = next(
        (
            line
            for line in lines
            if (PATENT_KEYWORD.search(line) or METHOD_KEYWORD.search(line))
            and not HEADING_LIST.search(line)
        ),
        "",
    )
    m = PATENT_NO.search(patent_line)
    patent_no = m.group(1) if m else patent_line
    return {"capital": capital, "record": record, "patent_no": patent_no}


def _all_missing(reason: str) -> dict:
    """모든 필드를 "찾지 못함" 상태로 채운다. 값과는 별개로 {field}_found=False 플래그를 같이 반환해,
    표시용 문자열(사유)과 "실제 값을 찾았는지"를 코드에서 구분할 수 있게 한다."""
    result = dict.fromkeys(FIELDS, reason)
    for f in FIELDS:
        result[f"{f}_found"] = False
    return result


def parse_files(paths: list[str], fetch_error: str | None = None) -> dict:
    if not paths:
        return _all_missing(fetch_error or DOWNLOAD_FAILED_REASON)

    combined = dict.fromkeys(FIELDS, "")
    extract_errors: list[str] = []
    any_text_extracted = False

    for path in paths:
        text, err = extract_text(path)
        if err:
            extract_errors.append(f"{os.path.basename(path)}: {err}")
            continue
        any_text_extracted = True
        for key, value in parse_requirements(text).items():
            if value and not combined[key]:
                combined[key] = value

    if not any_text_extracted:
        reason = "; ".join(extract_errors) if extract_errors else "첨부파일에서 텍스트를 추출하지 못함"
        return _all_missing(reason)

    result = {}
    for key in FIELDS:
        if combined[key]:
            result[key] = combined[key]
            result[f"{key}_found"] = True
        else:
            result[key] = NOT_FOUND_REASON
            result[f"{key}_found"] = False
    return result
