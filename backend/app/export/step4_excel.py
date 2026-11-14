"""4단계: 수집/파싱 결과를 화면용 행(to_row_dict)과 엑셀(build_excel)로 만든다.

화면의 "상태"는 사용자가 어떤 행을 확인해야 하는지 바로 알 수 있도록 3단계로 나눈다.
- 정상: 10개 컬럼이 모두 실제 값이고 값을 의심할 신호가 없음 -> 확인하지 않아도 됨
- 확인 필요: 공고문은 읽었지만 값을 못 찾았거나 오인 가능성이 있음 -> 근거 문장만 보면 됨
- 수집 실패: 페이지/첨부파일을 못 받았거나 공고문을 읽지 못함 -> 원문 링크로 직접 확인

엑셀은 의뢰처 양식(표지·리스트·분석표·상세분석)을 따른다. 상태는 화면에서 확인하고 엑셀은 받아서
손으로 고치는 용도라, 엑셀에는 상태 열·색·메모를 넣지 않고 분석표는 수식으로 리스트를 센다.
"""

import re
from collections import Counter
from datetime import date

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter

from app.crawler.step2_download import DETAIL_URL_TMPL
from app.parser.step3_parse import (
    CAPITAL_AMOUNT,
    CAPITAL_KEYWORD,
    NEEDS_CHECK_PREFIX,
    NOT_APPLICABLE,
    PATENT_NUMBER,
    amount_to_won,
    is_document_line,
    normalize_amount,
)

COLUMNS = [
    "공고일자", "아파트명", "공사명", "세대수", "자본금",
    "실적", "공법번호", "낙찰방법", "낙찰업체", "낙찰금액", "상태",
]

STATUS_OK = "정상"
STATUS_CHECK = "확인 필요"
STATUS_FAIL = "수집 실패"
# 공고문을 읽었고 관련 문구가 전혀 없을 때만 쓰는 표시. 못 읽었거나 불명확하면 사유·확인 필요가 그대로 남는다.
# 공법번호만 예외: 등록번호가 없으면 공법 이름이 있어도 미적용 (step3_parse.parse_files)
NOT_APPLICABLE_LABELS = {"자본금": "제한없음", "실적": "제한없음", "공법번호": "미적용"}

# 값 대신 "사유"가 들어갈 수 있는 컬럼 -> parse_files()가 반환하는 필드 키
PARSED_FIELDS = {"자본금": "capital", "실적": "record", "공법번호": "patent_no"}
# 웹 결과 표에 근거를 넘길 때 쓰는 키 접두사 (프런트는 "_"로 시작하는 키를 컬럼으로 보여주지 않음)
EVIDENCE_KEY = "_근거_"

# K-apt 목록의 단지명 뒤에 붙는 발주 주체 표기 (예: "서서울삼성아파트 (입대의)")
APT_SUFFIX = re.compile(r"\s*\(입대의\)\s*$")
RECORD_YEARS = re.compile(r"(?<!\d)(\d{1,2})\s*(?:개\s*)?년")
RECORD_HOUSEHOLDS = re.compile(r"(\d[\d,]*)\s*세대")
RECORD_COUNT = re.compile(r"(\d+)\s*(?:[건회]|개(?!\s*년))")
# 아파트 도로포장 입찰에서 상식적인 요건 범위. 벗어나면 숫자를 잘못 읽었을 가능성이 크다.
# ponytail: 고정 범위 -> 다른 공종(대형 공사 등)으로 넓히면 범위 조정 필요
CAPITAL_RANGE_EOK = (0.1, 50)
RECORD_MAX_COUNT = 30



def summarize_capital(sentence: str) -> str:
    """"4) 자본금 : 5억 이상인 업체" -> "5억원(500,000,000원)". 금액을 못 찾으면 원문 그대로."""
    keyword = CAPITAL_KEYWORD.search(sentence)
    m = CAPITAL_AMOUNT.search(sentence, keyword.end() if keyword else 0) or CAPITAL_AMOUNT.search(sentence)
    return normalize_amount(m.group()) if m else sentence


def summarize_record(sentence: str) -> str:
    """실적 요건 문장 -> "500세대 5건"(세대 조건이 있으면) 또는 "5년 5건". 건수를 못 찾으면 원문 그대로."""
    count = RECORD_COUNT.search(sentence)
    if not count:
        return sentence
    cond = RECORD_HOUSEHOLDS.search(sentence)
    cond = f"{cond.group(1)}세대" if cond else ""
    if not cond and (years := RECORD_YEARS.search(sentence)):
        cond = f"{years.group(1)}년"
    return f"{cond} {re.sub(r'\s+', '', count.group())}".strip()


# 확인 필요 사유를 사용자가 이해할 수 있도록, 값으로 인정하는 기준을 그대로 안내한다 (step3_parse의 규칙과 맞출 것)
FIELD_RULES = {
    "자본금": "자본금은 '자본금' 단어가 있는 줄에서, 같은 줄이나 바로 다음 2줄에 금액(예: 5억, 3천만원, 500,000,000원)이 있을 때만 값으로 인정해요.",
    "실적": (
        "실적 요건 줄은 두 가지만 인정해요. "
        "① '실적' 단어가 있고 건수·횟수·금액(예: 3건, 5회, 10억원)이 같은 줄이나 바로 다음 2줄에 있는 줄, "
        "② '최근 N년간 … M건 / M회 / M개 단지 이상' 형식의 줄이에요."
    ),
    "공법번호": "공법번호는 특허·실용신안 등록번호(10-1234567 또는 20-1234567 형식)가 공고문에 있을 때만 인정하고, 없으면 미적용으로 표시해요.",
}
MISSING_INFO_GUIDES = {
    "세대수": "세대수: K-apt 상세페이지에 세대수가 없거나 0으로 등록돼 있고, 공고문에서도 '단지규모 … N세대' 같은 문구를 찾지 못했어요.",
}
# 확인 필요 안내 문장(줄바꿈으로 구분). 웹 결과에서 행을 펼치면 보이고, 엑셀에서는 상태 칸 메모로 붙는다.
GUIDE_KEY = "_확인안내"


def _not_found_guide(r: dict, col: str, value: str) -> tuple[str, str]:
    """요건 값을 확정하지 못한 칸의 (짧은 사유, 안내)."""
    if value.startswith(NEEDS_CHECK_PREFIX):
        sentence = value.removeprefix(NEEDS_CHECK_PREFIX)
        return f"{col} 기준에 맞는 줄 없음", (
            f"{col}: {FIELD_RULES[col]} 이 공고문에는 기준에 맞는 줄이 없어, 가장 가까운 문장"
            f"('{sentence[:60]}')을 대신 보여드려요. 원문에서 {col} 요건을 직접 확인해 주세요."
        )
    if r.get("ocr_used"):
        why = "스캔 PDF는 앞 2쪽만 글자 인식으로 읽어 뒤쪽에 요건이 있을 수 있어"
    elif r.get("extract_errors"):
        why = "읽지 못한 첨부파일에 요건이 있을 수 있어"
    else:
        why = "공고문 다른 곳에 관련 단어(예: 최근 N년간, 공법, 신기술, 실용신안)가 있어"
    return f"{col} 관련 문구 없음", (
        f"{col}: 공고문에서 {col} 요건 문장을 찾지 못했어요. {why} '제한없음'으로 단정하지 않았어요. 원문을 확인해 주세요."
    )


def _suspicions(r: dict, capital: str, record: str) -> list[tuple[str, str]]:
    """값은 찾았지만 오인 가능성이 있는 신호들. (짧은 사유, 안내) 목록."""
    reasons = []
    if r.get("ocr_used"):
        reasons.append((
            "스캔 PDF(글자 인식)",
            "이 공고문은 스캔(이미지) PDF라 글자 인식으로 읽었어요. 글자 인식은 숫자·글자를 잘못 읽을 수 있어, "
            "값을 찾았더라도 원문과 한 번 대조해 주세요.",
        ))
    amounts = r.get("capital_amounts") or []
    if len(amounts) > 1:
        reasons.append((
            f"자본금 금액이 여러 개({', '.join(amounts)})",
            f"공고문에 자본금 금액이 여러 개({', '.join(amounts)}) 나와, 어느 금액이 참가 요건인지 정할 수 없어요.",
        ))
    # 요약된 자본금("5억원(500,000,000원)")의 첫 금액. 요약 전 원문·사유 문구에는 금액 형식이 맨 앞에 오지 않는다
    amount = CAPITAL_AMOUNT.match(capital)
    won = amount and amount_to_won(amount.group())
    if won and not CAPITAL_RANGE_EOK[0] <= won / 1e8 <= CAPITAL_RANGE_EOK[1]:
        reasons.append((
            f"자본금 {capital} 일반 범위 밖",
            f"자본금 {capital}은 일반적인 범위(1천만원~50억)를 벗어나, 숫자를 잘못 읽었을 수 있어요.",
        ))
    count = RECORD_COUNT.search(record)
    if count and not 1 <= int(count.group(1)) <= RECORD_MAX_COUNT:
        reasons.append((
            f"실적 {count.group()} 일반 범위 밖",
            f"실적 {count.group()}은 일반적인 범위(1~{RECORD_MAX_COUNT})를 벗어나, 숫자를 잘못 읽었을 수 있어요.",
        ))
    for col, key in (("자본금", "capital"), ("실적", "record")):
        sentence = r.get(key, "")
        if r.get(f"{key}_found") and is_document_line(sentence):
            reasons.append((
                f"{col}이 제출서류 문장에서 뽑힘",
                f"{col}: 참가자격 요건 줄을 찾지 못해 제출서류 목록(예: 실적증명서 1부)의 문장에서 값을 뽑았어요. "
                f"제출서류 문장은 요건이 아닐 수 있어요. {FIELD_RULES[col]}",
            ))
    return reasons


def _evidence(r: dict, key: str) -> str:
    """셀 메모로 보여줄 근거: 값을 뽑은 원문 문장 + 파일명."""
    sentence = r.get("patent_no_evidence") if key == "patent_no" else r.get(key, "")
    sentence = (sentence or "").removeprefix(NEEDS_CHECK_PREFIX)
    source = r.get(f"{key}_source", "")
    if not source or not sentence or sentence == NOT_APPLICABLE:
        return ""
    return f"근거: {sentence}\n파일: {source}"


def to_row_dict(r: dict) -> dict:
    """수집 결과 1건을 엑셀 컬럼과 동일한 순서의 dict로 변환.

    엑셀 생성과 API 응답(프런트 결과 테이블)이 같은 컬럼 매핑을 쓰도록 공유.
    """
    # 값을 확정적으로 찾은 경우만 요약. "확인 필요"/사유 문구는 사람이 봐야 하므로 원문 유지.
    capital, record = r.get("capital", ""), r.get("record", "")
    if r.get("capital_found") and capital != NOT_APPLICABLE:
        capital = summarize_capital(capital)
    if r.get("record_found") and record != NOT_APPLICABLE:
        record = summarize_record(record)

    values = {
        "공고일자": r.get("announce_date", "")[:10],
        "아파트명": APT_SUFFIX.sub("", r.get("apt_name") or r.get("apt_name_list", "")),
        "공사명": r.get("work_name") or r.get("bid_title", ""),
        "세대수": r.get("household_count") or r.get("notice_household", ""),
        "자본금": capital,
        "실적": record,
        "공법번호": r.get("patent_no", ""),
        "낙찰방법": r.get("bid_method", ""),
        "낙찰업체": r.get("winner", ""),
        "낙찰금액": r.get("bid_amount") or r.get("winner_amount", ""),
    }
    for col, label in NOT_APPLICABLE_LABELS.items():
        if values[col] == NOT_APPLICABLE:
            values[col] = label

    if not r.get("success", True):
        # 브라우저 오류는 뒤에 여러 줄짜리 내부 로그가 붙어 첫 줄만 보여준다
        status = f"{STATUS_FAIL}: {(r.get('error') or '알 수 없는 오류').splitlines()[0]}"
    elif r.get("extract_failed"):
        status = f"{STATUS_FAIL}: {r.get('capital', '공고문을 읽지 못함')}"
    else:
        reasons = []
        for col, value in values.items():
            key = PARSED_FIELDS.get(col)
            if key is None:
                if not value:
                    guide = MISSING_INFO_GUIDES.get(col, f"{col}: K-apt 목록·상세페이지에 {col} 값이 없어요.")
                    reasons.append((f"{col} 정보 없음", guide))
            elif not r.get(f"{key}_found", bool(value)):
                reasons.append(_not_found_guide(r, col, value))
        reasons += _suspicions(r, capital, record)
        if r.get("extract_errors"):
            errors = "; ".join(r["extract_errors"])
            reasons.append((
                "일부 첨부파일을 읽지 못함",
                f"첨부파일 중 일부를 읽지 못했어요({errors}). 읽지 못한 파일에 요건이 있을 수 있어요.",
            ))
        status = f"{STATUS_CHECK}: " + " / ".join(short for short, _ in reasons) if reasons else STATUS_OK
        if reasons:
            values[GUIDE_KEY] = "\n".join(guide for _, guide in reasons)

    bid_num = r.get("bid_num")
    values["공고 링크"] = DETAIL_URL_TMPL.format(bid_num=bid_num) if bid_num else ""
    values["상태"] = status
    for col, key in PARSED_FIELDS.items():
        if evidence := _evidence(r, key):
            values[EVIDENCE_KEY + col] = evidence
    if not r.get("household_count") and r.get("notice_household"):
        values[EVIDENCE_KEY + "세대수"] = f"K-apt에 세대수가 없어 공고문에서 읽은 값\n파일: {r.get('notice_household_source', '')}"
    return values


# ---------------- 엑셀: 의뢰처 양식 (표지 · 리스트 · 분석표 · 상세분석) ----------------

SHEET_COLUMNS = ["no", "공고일자", "아파트명", "공사명", "세대수", "자본금", "실적", "공법번호", "낙찰방법", "낙찰업체", "낙찰금액"]
LIST_COL = {name: get_column_letter(i) for i, name in enumerate(SHEET_COLUMNS, start=1)}
LIST_FIRST_ROW = 3
# 의뢰처 분석표의 자본금 구간(억). 데이터에 다른 금액이 있으면 그 금액 행이 더해진다.
CAPITAL_BINS_EOK = [1, 1.5, 2, 3, 4, 4.5, 5, 6, 7, 8, 10, 15]
CAPITAL_RANGES_EOK = [(1, 3), (3, 5), (5, 7), (7, 9)]
HOUSEHOLD_RANGES = [(None, 100), (100, 250), (250, 500), (500, 1000), (1000, 3000), (3000, None)]
METHODS = ["최저가", "적격심사"]
# 값이 확정되지 않은 칸(확인 필요 사유·수집 실패 문구)은 숫자·요약값이 아니어서 어느 구간에도 세지 않고 따로 센다
UNSURE = "확인 필요"
RECORD_SUMMARY = re.compile(r"^(?:\d+(?:세대|년) )?\d+[건회개]$")


def _record_order(value: str) -> tuple:
    """"300세대 5건" < "1000세대 3건" < "5년 1건" 처럼 세대 조건 먼저, 숫자 크기 순."""
    nums = [int(n) for n in re.findall(r"\d+", value)]
    return ("세대" not in value, "년" not in value, *nums)
WINNER_HIGHLIGHT_COUNT = 3

COMMA_FORMAT = '_-* #,##0_-;\\-* #,##0_-;_-* "-"_-;_-@_-'
RATIO_FORMAT = "0.0%"
THIN = Side(style="thin")
MEDIUM = Side(style="medium")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TITLE_BOX = Border(left=MEDIUM, right=MEDIUM, top=MEDIUM, bottom=MEDIUM)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _number(text: str):
    """"95,900,000" -> 95900000. 숫자가 아니면 글자 그대로(빈 값은 None)."""
    digits = re.sub(r"[,\s원]", "", text or "")
    return int(digits) if digits.isdigit() else (text or None)


def to_sheet_row(no: int, rd: dict) -> list:
    """화면용 행 -> 리스트 시트 행. 의뢰처 양식대로 자본금은 억 단위 숫자, 요건 없음(제한없음·미적용)은 빈칸."""
    capital = rd["자본금"]
    amount = CAPITAL_AMOUNT.match(capital)
    if won := amount and amount_to_won(amount.group()):
        capital = won / 10**8
        capital = int(capital) if capital.is_integer() else capital
    elif capital == "제한없음":
        capital = None
    try:
        day = date.fromisoformat(rd["공고일자"])
    except ValueError:
        day = rd["공고일자"] or None
    record, patent = rd["실적"], rd["공법번호"]
    return [
        no, day, rd["아파트명"], rd["공사명"], _number(rd["세대수"]), capital,
        None if record == "제한없음" else record or None,
        None if patent == "미적용" else patent or None,
        rd["낙찰방법"] or None, rd["낙찰업체"] or None, _number(rd["낙찰금액"]),
    ]


def _scope(cond: dict) -> str:
    regions = cond["regions"]
    return "수도권" if set(regions) == {"서울", "인천", "경기"} else "·".join(regions)


def _years(cond: dict) -> str:
    a, b = cond["date_start"].year, cond["date_end"].year
    return f"{a}" if a == b else f"{a}~{b}"


def export_filename(cond: dict) -> str:
    """의뢰처 파일 이름 형식: "2026년도 수도권 아스콘 낙찰리스트(2026.01.01~2026.11.03).xlsx"."""
    name = (
        f"{_years(cond)}년도 {_scope(cond)} {cond['keyword']} 낙찰리스트"
        f"({cond['date_start']:%Y.%m.%d}~{cond['date_end']:%Y.%m.%d}).xlsx"
    )
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def _crit(text: str) -> str:
    """COUNTIF 조건으로 쓸 '글자 그대로 일치' 문자열 (와일드카드·비교 기호를 글자로 취급)."""
    escaped = text.replace("~", "~~").replace("*", "~*").replace("?", "~?").replace('"', '""')
    return f'"={escaped}"'


def _rng(col: str, last: int) -> str:
    c = LIST_COL[col]
    return f"리스트!${c}${LIST_FIRST_ROW}:${c}${last}"


def _put(ws, row: int, col: int, value, font: Font, fmt: str | None = None, border=BOX):
    cell = ws.cell(row=row, column=col, value=value)
    cell.font, cell.border, cell.alignment = font, border, CENTER
    if fmt:
        cell.number_format = fmt
    return cell


def _table(ws, top: int, left: int, header: list, rows: list[list], font: Font, fmts: list | None = None) -> int:
    """머리글 + 행을 테두리 칸으로 쓰고, 다음에 쓸 수 있는 행 번호를 돌려준다."""
    for i, h in enumerate(header):
        _put(ws, top, left + i, h, Font(name=font.name, size=font.sz, bold=True))
    for r, values in enumerate(rows, start=top + 1):
        for i, v in enumerate(values):
            _put(ws, r, left + i, v, font, (fmts or [None] * len(values))[i])
    return top + 1 + len(rows)


def _title(ws, cell_range: str, text: str, font: Font, border=None):
    ws.merge_cells(cell_range)
    first = ws[cell_range.split(":")[0]]
    first.value, first.font, first.alignment = text, font, CENTER
    if border:
        for row in ws[cell_range]:
            for cell in row:
                cell.border = border


def _widths(ws, widths: dict):
    for col, width in widths.items():
        ws.column_dimensions[col].width = width


def _patent_numbers(sheet_rows: list[list]) -> list[str]:
    """리스트에 나온 공법 번호를 많이 쓰인 순으로. 한 공고에 여러 번호면 번호마다 센다."""
    counts = Counter()
    for row in sheet_rows:
        counts.update(list(dict.fromkeys(f"{a}-{b}" for a, b in PATENT_NUMBER.findall(row[7] or ""))))
    return [no for no, _ in counts.most_common()]


def _methods(sheet_rows: list[list]) -> list[str]:
    found = [row[8] for row in sheet_rows if row[8]]
    return METHODS + sorted(set(found) - set(METHODS))


def _write_cover(ws, cond: dict | None):
    font = "맑은 고딕"
    if cond:
        title = f"{_years(cond)}년도 {_scope(cond)} 공동주택 {cond['keyword']} 입찰 낙찰공고 리스트"
        detail = (
            "[http://www.k-apt.go.kr/-입찰정보-사업자 선정(경쟁입찰)결과 공개\n"
            # 입찰공고일은 의뢰처가 직접 채운다 (2026-11-14 요청). 표지 이미지(K-apt 검색 화면)도 매번 직접 넣는다.
            f"지역-({','.join(cond['regions'])})-입찰공고명({cond['keyword']})-입찰공고일 -낙찰공고- 검색]"
        )
    else:
        title, detail = "공동주택 입찰 낙찰공고 리스트", "[http://www.k-apt.go.kr/-입찰정보-사업자 선정(경쟁입찰)결과 공개]"
    _title(ws, "A2:H2", title, Font(name=font, size=24, bold=True), BOX)
    _title(ws, "A3:H3", detail, Font(name=font, size=12), BOX)
    ws.row_dimensions[2].height, ws.row_dimensions[3].height = 73.5, 45
    _widths(ws, {"A": 5.8, "B": 8.6, "C": 36.8, "D": 7.6, "E": 7.6, "F": 15.9, "G": 32, "H": 16.5})


def _write_list(ws, sheet_rows: list[list], cond: dict | None):
    font = Font(name="굴림", size=12)
    title = f"{', '.join(cond['regions'])} {cond['keyword']} 공사 업체선정 결과 ({_years(cond)}년)" if cond else "업체선정 결과"
    _title(ws, "A1:K1", title, Font(name="굴림", size=20, bold=True))
    ws.row_dimensions[1].height, ws.row_dimensions[2].height = 43.5, 27
    for i, name in enumerate(SHEET_COLUMNS, start=1):
        _put(ws, 2, i, name, font)
    fmts = {2: 'm"/"d;@', 5: COMMA_FORMAT, 11: COMMA_FORMAT}
    for r, values in enumerate(sheet_rows, start=LIST_FIRST_ROW):
        ws.row_dimensions[r].height = 35.1
        for i, v in enumerate(values, start=1):
            # openpyxl은 제어 문자가 든 글을 저장하지 못하고 예외를 낸다 -> 한 칸 때문에 수집 전체가 실패하지 않게 지운다
            cell = _put(ws, r, i, ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v, font, fmts.get(i))
            # openpyxl은 "="로 시작하는 글을 수식으로 저장한다 -> 공고문·K-apt 글은 항상 글자로 저장
            if cell.data_type == "f":
                cell.data_type = "s"
    ws.auto_filter.ref = f"A2:K{LIST_FIRST_ROW + len(sheet_rows) - 1}"
    _widths(ws, {"A": 5.8, "B": 8.6, "C": 47.6, "D": 88.5, "E": 9.6, "F": 7.6, "G": 15.9, "H": 32.1, "I": 16.5, "J": 25.8, "K": 20.5})


def _count_rows(labels_and_formulas: list[tuple[str, str]], total: str, first_row: int, count_col: str) -> list[list]:
    """(구분, 건수 수식) 목록 + 확인 필요(나머지) + 합계 행. 비율은 합계 대비."""
    last = first_row + len(labels_and_formulas) - 1
    rows = [[label, f"={f}", f"={count_col}{first_row + i}/{total}"] for i, (label, f) in enumerate(labels_and_formulas)]
    rows.append([UNSURE, f"={total}-SUM({count_col}{first_row}:{count_col}{last})", f"={count_col}{last + 1}/{total}"])
    rows.append(["합계", f"={total}", None])
    return rows


def _write_analysis(ws, sheet_rows: list[list], last: int):
    """의뢰처 '분석표' 시트: 낙찰방법 · 자본금 · 공법 건수와 비율, 공법 번호별 건수. 모두 리스트를 세는 수식."""
    font, small = Font(name="굴림체", size=15), Font(name="굴림체", size=12)
    total = f"ROWS({_rng('no', last)})"
    fmts = [None, None, RATIO_FORMAT]
    capital_bins = sorted(set(CAPITAL_BINS_EOK) | {v[5] for v in sheet_rows if isinstance(v[5], (int, float))})
    sections = [
        ("낙찰방법", [(m, f"COUNTIF({_rng('낙찰방법', last)},{_crit(m)})") for m in _methods(sheet_rows)]),
        ("자본금", [("제한없음", f"COUNTBLANK({_rng('자본금', last)})")]
         + [(f"{b:g}억", f"COUNTIF({_rng('자본금', last)},{b})") for b in capital_bins]),
        # 공법 번호는 10-/20-으로 시작한다. 빈칸은 미적용, 그 밖의 글(사유 문구)은 확인 필요
        ("공법", [("적용", f'COUNTIF({_rng("공법번호", last)},"?0-*")'), ("미적용", f"COUNTBLANK({_rng('공법번호', last)})")]),
    ]
    row = 2
    for title, items in sections:
        _put(ws, row, 3, title, font, border=Border())
        rows = _count_rows(items, total, row + 2, "D")
        row = _table(ws, row + 1, 3, ["", "건수", "비율"], rows, font, fmts) + 1

    _put(ws, 2, 7, "공법번호", small, border=Border())
    patent_rows = [
        [i, no, None, None, f'=COUNTIF({_rng("공법번호", last)},"*{no}*")']
        for i, no in enumerate(_patent_numbers(sheet_rows), start=1)
    ]
    _table(ws, 3, 7, ["no", "공법 번호", "특 허 명 칭", "출원자", "건수"], patent_rows, small)
    _widths(ws, {"A": 0.6, "B": 5.1, "C": 15, "D": 12, "E": 16, "F": 6.1, "G": 8, "H": 18, "I": 80, "J": 24, "K": 12})


def _write_detail(ws, sheet_rows: list[list], last: int):
    """의뢰처 '분석표_이정원' 시트: 세대수 대비 낙찰금액, 업체별 수주, 참가자격(실적·자본금), 공법 선호도."""
    font = Font(name="맑은 고딕", size=11)
    bold = Font(name="맑은 고딕", size=11, bold=True)
    households, amounts = _rng("세대수", last), _rng("낙찰금액", last)
    total = f"ROWS({_rng('no', last)})"

    # 1. 규모(세대수) 대비 낙찰 단가
    _title(ws, "A1:H1", "1. 규모(세대수) 대비 낙찰 단가 분석표", bold, TITLE_BOX)
    _title(ws, "A2:H2", "- 세대수 대비하여 평균 낙찰금액 얼마인지?", font, TITLE_BOX)
    _put(ws, 4, 2, "단지규모 구간 구하기", bold, border=Border())
    for r, (label, fn) in enumerate((("최소", "MIN"), ("최대", "MAX"), ("중간값", "MEDIAN")), start=5):
        _put(ws, r, 2, label, font)
        _put(ws, r, 3, f"=IFERROR({fn}({households}),0)", font, COMMA_FORMAT)
    rows = []
    for i, (lo, hi) in enumerate(HOUSEHOLD_RANGES):
        conds = [f'{households},">={lo}"'] * (lo is not None) + [f'{households},"<{hi}"'] * (hi is not None)
        label = f"{hi}세대 미만" if lo is None else f"{lo}세대~" if hi is None else f"{lo}~{hi}세대"
        r = 10 + i
        rows.append([label, f"=COUNTIFS({','.join(conds)})", f"=IFERROR(AVERAGEIFS({amounts},{','.join(conds)}),0)", f"=D{r}/10000"])
    rows.append(["총합계", f"=SUM(C10:C{9 + len(HOUSEHOLD_RANGES)})", None, None])
    _table(ws, 9, 2, ["단지 규모", "단지수", "평균 낙찰금액", "평균 낙찰금액(만 원)"], rows, font,
           [None, None, COMMA_FORMAT, '#,##0"만원"'])

    # 2. 업체별 수주 점유율
    _title(ws, "A19:H19", "2. 업체별 수주 점유율", bold, TITLE_BOX)
    winners = _rng("낙찰업체", last)
    counts = Counter(v[9] for v in sheet_rows if v[9])
    names = sorted(counts)
    rows = [[i, n, f"=COUNTIF({winners},{_crit(n)})"] for i, n in enumerate(names, start=1)]
    rows.append([None, "총합계", f"=SUM(C22:C{21 + len(names)})"])
    _table(ws, 21, 1, ["no", "낙찰업체", "낙찰 건수"], rows, font)
    many = [n for n in names if counts[n] >= WINNER_HIGHLIGHT_COUNT]
    rows = [
        [n, f"=COUNTIF({winners},{_crit(n)})", f"=SUMIF({winners},{_crit(n)},{amounts})", f"=IFERROR(AVERAGEIF({winners},{_crit(n)},{amounts}),0)"]
        for n in many
    ] or [[f"({WINNER_HIGHLIGHT_COUNT}건 이상 낙찰 업체 없음)", None, None, None]]
    end = 22 + len(rows)
    rows.append(["총합계", f"=SUM(F22:F{end - 1})", f"=SUM(G22:G{end - 1})", f"=IFERROR(G{end}/F{end},0)"])
    _table(ws, 21, 5, ["업체", f"{WINNER_HIGHLIGHT_COUNT}건 이상 낙찰", "합계 : 낙찰금액", "평균 : 낙찰금액"], rows, font,
           [None, None, COMMA_FORMAT, COMMA_FORMAT])

    # 3. 제한경쟁입찰 참가자격 (실적 요건 x 낙찰방법, 자본금 구간)
    _title(ws, "J1:O1", "3. 제한경쟁입찰 참가자격 분석표", bold, TITLE_BOX)
    _title(ws, "J2:O2", "사업실적, 기술능력, 자본금 비율 및 건수 비교", font, TITLE_BOX)
    records, methods_rng = _rng("실적", last), _rng("낙찰방법", last)
    methods = _methods(sheet_rows)
    values = sorted({v[6] for v in sheet_rows if v[6] and RECORD_SUMMARY.match(v[6])}, key=_record_order)
    first, cols = 6, [get_column_letter(11 + i) for i in range(len(methods))]
    total_col, ratio_col = get_column_letter(11 + len(methods)), get_column_letter(12 + len(methods))
    end = first + len(values) + 2  # 실적 행 + 제한없음 + 확인 필요 다음이 총합계
    rows = []
    for i, (label, crit) in enumerate([*((v, _crit(v)) for v in values), ("제한없음", '""')]):
        r = first + i
        rows.append([label, *(f"=COUNTIFS({records},{crit},{methods_rng},{_crit(m)})" for m in methods),
                     f"=SUM({cols[0]}{r}:{cols[-1]}{r})", f"={total_col}{r}/{total_col}{end}"])
    r = end - 1
    rows.append([UNSURE, *(f"=COUNTIF({methods_rng},{_crit(m)})-SUM({c}{first}:{c}{r - 1})" for m, c in zip(methods, cols)),
                 f"=SUM({cols[0]}{r}:{cols[-1]}{r})", f"={total_col}{r}/{total_col}{end}"])
    rows.append(["총합계", *(f"=SUM({c}{first}:{c}{r})" for c in [*cols, total_col]), f"={total_col}{end}/{total_col}{end}"])
    _table(ws, first - 1, 10, ["실적 요건", *methods, "총합계", "비율"], rows, font, [None] * (len(methods) + 2) + [RATIO_FORMAT])
    _put(ws, end + 1, 10, "낙찰방법이 빈 공고는 실적 표에서 빠집니다.", Font(name="맑은 고딕", size=9, color="FF6C757D"), border=Border())

    capitals = _rng("자본금", last)
    top = end + 3
    items = [("제한 없음", f"COUNTBLANK({capitals})"), ("1억 미만", f'COUNTIF({capitals},"<1")')]
    items += [(f"{lo}억~{hi}억 미만", f'COUNTIFS({capitals},">={lo}",{capitals},"<{hi}")') for lo, hi in CAPITAL_RANGES_EOK]
    items += [(f"{CAPITAL_RANGES_EOK[-1][1]}억 이상", f'COUNTIF({capitals},">={CAPITAL_RANGES_EOK[-1][1]}")')]
    rows = [[label, *rest] for label, *rest in _count_rows(items, total, top + 1, "K")]
    _table(ws, top, 10, ["자본금구간", "공고 수", "비율"], rows, font, [None, None, RATIO_FORMAT])

    # 4. 적용공법 선호도
    _title(ws, "Q1:U1", "4. 적용공법 선호도", bold, TITLE_BOX)
    _title(ws, "Q2:U2", "특허공법 선정비율", font, TITLE_BOX)
    _title(ws, "Q4:S4", "특허공법 적용시 낙찰 비율", bold)
    patents = _rng("공법번호", last)
    rows = _count_rows([("적용", f'COUNTIF({patents},"?0-*")'), ("미적용", f"COUNTBLANK({patents})")], total, 6, "R")
    _table(ws, 5, 17, ["구분", "건수", "비율"], rows, font, [None, None, RATIO_FORMAT])
    numbers = _patent_numbers(sheet_rows)
    end = 13 + len(numbers)
    rows = [[no, f'=COUNTIF({patents},"*{no}*")', f"=IFERROR(R{13 + i}/R${end},0)", None, None] for i, no in enumerate(numbers)]
    rows.append(["총합계", f"=SUM(R13:R{end - 1})", None, None, None])
    _table(ws, 12, 17, ["공법 번호", "건수", "비율", "특 허 명 칭", "출원자"], rows, font, [None, None, RATIO_FORMAT, None, None])

    _widths(ws, {"A": 6, "B": 24, "C": 14, "D": 16, "E": 22, "F": 14, "G": 18, "H": 18, "I": 3,
                 "J": 18, "K": 12, "L": 12, "M": 12, "N": 12, "O": 10, "P": 3, "Q": 16, "R": 10, "S": 10, "T": 60, "U": 24})


def build_excel(row_dicts: list[dict], out_path: str, cond: dict | None = None) -> None:
    """to_row_dict()로 변환된 행들을 의뢰처 양식 엑셀로 저장. cond: regions·keyword·date_start·date_end (표지·제목용)."""
    sheet_rows = [to_sheet_row(i, rd) for i, rd in enumerate(row_dicts, start=1)]
    last = LIST_FIRST_ROW + max(len(sheet_rows), 1) - 1
    wb = Workbook()
    wb.active.title = "표지"
    _write_cover(wb.active, cond)
    _write_list(wb.create_sheet("리스트"), sheet_rows, cond)
    _write_analysis(wb.create_sheet("분석표"), sheet_rows, last)
    _write_detail(wb.create_sheet("상세분석"), sheet_rows, last)
    wb.active = 1
    wb.save(out_path)
