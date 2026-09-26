"""3단계: 첨부 공고문(PDF/HWP) 텍스트 파싱 -> 자본금/실적/공법번호 추출.

값을 못 찾았을 때는 빈 문자열이 아니라 "왜 없는지"를 그 자리에 채운다
(첨부파일 자체가 없는지 / 다운로드가 실패했는지 / 텍스트 추출이 안 됐는지 / 문서에 해당 문구가 없는지).
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

import pdfplumber
from bs4 import BeautifulSoup

FIELDS = ["capital", "record", "patent_no"]

# hwp5html은 pyhwp가 파이썬과 같은 bin 폴더에 설치하는 CLI. venv를 activate하지 않고 서버를
# 띄우면 PATH에 없어서 모든 HWP가 실패하므로, 현재 인터프리터 옆의 실행파일을 우선 사용한다.
# (shutil.which는 Windows의 hwp5html.exe 확장자도 처리한다)
HWP5HTML = shutil.which("hwp5html", path=os.path.dirname(sys.executable)) or "hwp5html"

# 공고문 표 헤더에 "자 본 금", "실 적"처럼 글자 사이를 띄어 쓰는 관행이 매우 흔해
# (실제 631개 첨부파일 스캔 결과 자본금 미검출 사례의 대다수가 이 패턴이었음) 글자 사이
# 공백을 허용하는 정규식으로 키워드를 찾는다.
CAPITAL_KEYWORD = re.compile(r"자\s*본\s*금")
RECORD_KEYWORD = re.compile(r"실\s*적")
PATENT_KEYWORD = re.compile(r"특\s*허")
METHOD_KEYWORD = re.compile(r"공\s*법")
# 한국 특허(10-)/실용신안(20-) 등록번호 형식. 전화번호(010-...)·사업자번호에 걸리지 않도록 앞뒤 경계를 둔다.
PATENT_NUMBER = re.compile(r"(?<![\d-])([12]0)\s*-\s*(\d{7})(?!\d)")
# "실적" 단어 없이 "최근 N년간 ... M건 이상"으로만 표현되는 공고문 대비 폴백 패턴
RECORD_FALLBACK = re.compile(r"최근\s*\d+\s*년간.*?\d+\s*건\s*이상")
RECENT_YEARS = re.compile(r"최근\s*\d+\s*년")

# 요건이 "없다"고 판단할 때 쓰는 넓은 키워드. 이 중 하나라도 문서에 있으면 "해당 없음"으로 단정하지
# 않는다 (특허 대신 "신기술 지정", "실용신안"으로 요건을 거는 공고 대비).
ABSENCE_CHECK = {
    "capital": CAPITAL_KEYWORD,
    "record": re.compile(r"실\s*적|최근\s*\d+\s*년간"),
    "patent_no": re.compile(r"특\s*허|공\s*법|신\s*기\s*술|실\s*용\s*신\s*안|[12]0\s*-\s*\d{7}"),
}
# PDF 추출 텍스트에 섞이는 폭 없는 문자(예: "5\u2060억"). 정규식 매칭을 깨뜨리므로 제거한다.
INVISIBLE_CHARS = re.compile(r"[\u200b-\u200d\u2060\ufeff]")

# "정상"으로 인정할 값의 조건. 키워드가 있는 줄이어도 이 조건을 못 채우면(예: "자본금 증명서 제출",
# "공법 기술사용 협약서") 요건 값이 아니라고 보고 "확인 필요"로 넘긴다 -> "정상" 행은 믿어도 되게 함.
CAPITAL_AMOUNT = re.compile(r"\d[\d,.]*\s*(?:억|천\s*만|백\s*만|만)|\d{1,3}(?:,\d{3})+\s*원")
RECORD_VALUE = re.compile(r"\d+\s*(?:건|회)|\d[\d,.]*\s*(?:억|천\s*만|백\s*만|만)\s*원?")

DOWNLOAD_FAILED_REASON = "공고문을 확보하지 못해 확인 불가"
NOT_FOUND_REASON = "공고문 내용에서 해당 항목 관련 문구를 찾지 못함"
# 관련 문구는 찾았지만 값(금액/건수/특허번호)이 확인되지 않은 경우 셀 앞에 붙는 표시
NEEDS_CHECK_PREFIX = "[확인 필요] "
# 공고문 전체에 관련 문구가 전혀 없으면 요건이 없는 공고로 본다 (실제 공고 25건 중 9건이 특허/공법 요건 없음)
NOT_APPLICABLE = "해당 없음(공고문에 관련 요건 없음)"


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
            try:
                result = subprocess.run(
                    [HWP5HTML, "--output", tmpdir, path], capture_output=True, text=True
                )
            except FileNotFoundError:
                return "", "HWP 변환 도구(hwp5html)를 찾지 못함(pyhwp 설치 확인 필요)"
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


def _merge_until_digit(lines: list[str], i: int) -> str:
    """값에 해당하는 숫자가 없으면 숫자가 나올 때까지 최대 2줄을 이어붙인다.

    표를 hwp5html로 변환하면 "자본금 :" 라벨과 "5억원 이상" 값이 서로 다른 줄(표 셀)로
    쪼개지는 문서가 있다 (실제 631개 파일 검증 중 발견). (번호 매김 "1)"의 숫자는 값으로 치지 않음)
    """
    merged = lines[i]
    if _has_value_digit(merged):
        return merged
    for nxt in lines[i + 1 : i + 3]:
        merged = f"{merged} {nxt}"
        if _has_value_digit(nxt):
            break
    return merged


def _find_line(lines: list[str], matches, is_valid, continues_from=None) -> tuple[str, bool]:
    """키워드 줄 중 값 조건(is_valid)을 만족하는 첫 줄을 찾는다. (줄, 조건 만족 여부) 반환.

    첫 키워드 줄이 "자본금 증명서 제출"처럼 값이 없는 줄이거나, "실적 5" / "건 이상"처럼
    값이 다음 줄로 잘린 경우가 있어, 최대 2줄까지 이어붙여 보며 조건을 만족할 때까지 계속 찾는다.
    끝까지 못 찾으면 첫 키워드 줄을 "확인 필요" 후보로 반환한다.
    항목 이름만 나열한 제목 줄(HEADING_LIST)은 후보에서 제외한다.

    continues_from: 키워드가 줄바꿈된 뒷줄에만 있는 경우("라. 최근 5년간 ... 보수공사(" / "실적) 3건 이상")
    바로 앞줄이 이 패턴을 포함하고 현재 줄이 새 번호 항목이 아니면 앞줄을 붙여 요건 문장 전체를 얻는다.
    """
    fallback = ""
    for i, line in enumerate(lines):
        if not matches(line) or HEADING_LIST.search(line):
            continue
        merged = line
        if (
            continues_from
            and i > 0
            and continues_from.search(lines[i - 1])
            and not continues_from.search(line)
            and not LEADING_MARKER.match(line)
        ):
            merged = f"{lines[i - 1]} {line}"
        for nxt in [None, *lines[i + 1 : i + 3]]:
            if nxt is not None:
                merged = f"{merged} {nxt}"
            if is_valid(merged):
                return merged, True
        fallback = fallback or _merge_until_digit(lines, i)
    return fallback, False


def parse_requirements(text: str) -> dict:
    """공고문 텍스트에서 자본금/실적/공법번호 요건을 추출.

    자본금/실적은 수치를 엄격히 정규화하지 않고 요건 문장 전체를 값으로 사용 -> 표현이 제각각인
    공고문에서도 사람이 바로 읽을 수 있는 값을 얻는다. 각 필드마다 {field}_found로
    값 조건(금액/건수/특허번호)을 만족했는지 함께 반환한다.
    """
    text = INVISIBLE_CHARS.sub("", text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    capital, capital_ok = _find_line(lines, CAPITAL_KEYWORD.search, CAPITAL_AMOUNT.search)
    record, record_ok = _find_line(
        lines,
        lambda line: RECORD_KEYWORD.search(line) or RECORD_FALLBACK.search(line),
        RECORD_VALUE.search,
        continues_from=RECENT_YEARS,
    )

    # 특허번호는 "공법" 줄과 떨어진 줄(다음 줄 괄호, 제출서류 항목 등)에 적히는 경우가 많아
    # (실제 공고문 7건 중 3건) 키워드 줄이 아니라 문서 전체에서 등록번호 형식을 찾는다.
    numbers = dict.fromkeys(f"{a}-{b}" for a, b in PATENT_NUMBER.findall("\n".join(lines)))
    if numbers:
        patent_no, patent_ok = ", ".join(numbers), True
    else:
        # 번호 없이 공법 이름만 있는 경우 -> 해당 줄을 "확인 필요"로 보여준다
        patent_no = next(
            (
                line
                for line in lines
                if (PATENT_KEYWORD.search(line) or METHOD_KEYWORD.search(line))
                and not HEADING_LIST.search(line)
            ),
            "",
        )
        patent_ok = False

    result = {
        "capital": capital, "capital_found": capital_ok,
        "record": record, "record_found": record_ok,
        "patent_no": patent_no, "patent_no_found": patent_ok,
    }
    for key, pattern in ABSENCE_CHECK.items():
        result[f"{key}_absent"] = not pattern.search(text)
    return result


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

    # 필드별 (값, 조건 만족 여부). 여러 첨부파일 중 조건을 만족하는 값을 우선한다.
    combined: dict[str, tuple[str, bool]] = dict.fromkeys(FIELDS, ("", False))
    # 모든 첨부파일에 관련 문구가 전혀 없을 때만 "해당 없음" (추출 실패한 파일이 하나라도 있으면 단정 불가)
    absent = dict.fromkeys(FIELDS, True)
    extract_errors: list[str] = []
    any_text_extracted = False

    for path in paths:
        text, err = extract_text(path)
        if err:
            extract_errors.append(f"{os.path.basename(path)}: {err}")
            continue
        any_text_extracted = True
        parsed = parse_requirements(text)
        for key in FIELDS:
            absent[key] = absent[key] and parsed[f"{key}_absent"]
            value, found = parsed[key], parsed[f"{key}_found"]
            cur_value, cur_found = combined[key]
            if value and (not cur_value or (found and not cur_found)):
                combined[key] = (value, found)

    if not any_text_extracted:
        reason = "; ".join(extract_errors) if extract_errors else "첨부파일에서 텍스트를 추출하지 못함"
        return _all_missing(reason)

    result = {}
    for key in FIELDS:
        value, found = combined[key]
        if found:
            result[key] = value
            result[f"{key}_found"] = True
        elif value:
            result[key] = NEEDS_CHECK_PREFIX + value
            result[f"{key}_found"] = False
        elif absent[key] and not extract_errors:
            result[key] = NOT_APPLICABLE
            result[f"{key}_found"] = True
        else:
            result[key] = NOT_FOUND_REASON
            result[f"{key}_found"] = False
    return result
