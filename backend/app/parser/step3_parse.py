"""3단계: 첨부 공고문(PDF/HWP/HWPX) 텍스트 파싱 -> 자본금/실적/공법번호 추출.

값을 못 찾았을 때는 빈 문자열이 아니라 "왜 없는지"를 그 자리에 채운다
(첨부파일 자체가 없는지 / 다운로드가 실패했는지 / 텍스트 추출이 안 됐는지 / 문서에 해당 문구가 없는지).
"""

import io
import logging
import os
import re
import zipfile
from contextlib import closing
from functools import lru_cache
from xml.etree import ElementTree

import pdfplumber
from bs4 import BeautifulSoup
from hwp5.hwp5html import HTMLTransform
from hwp5.xmlmodel import Hwp5File

FIELDS = ["capital", "record", "patent_no"]

# pyhwp는 공고문마다 "undefined UnderlineStyle value" 같은 경고를 대량으로 찍는다 (결과에는 영향 없음)
logging.getLogger("hwp5").setLevel(logging.ERROR)

# 공고문 표 헤더에 "자 본 금", "실 적"처럼 글자 사이를 띄어 쓰는 관행이 매우 흔해
# (실제 631개 첨부파일 스캔 결과 자본금 미검출 사례의 대다수가 이 패턴이었음) 글자 사이
# 공백을 허용하는 정규식으로 키워드를 찾는다.
CAPITAL_KEYWORD = re.compile(r"자\s*본\s*금")
RECORD_KEYWORD = re.compile(r"실\s*적")
PATENT_KEYWORD = re.compile(r"특\s*허")
METHOD_KEYWORD = re.compile(r"공\s*법")
# "유화 아스팔트 시공 가능 업체", "기술능력 : ... 조성물"처럼 공법·특허라는 말 없이 거는 공법 요건
METHOD_HINT = re.compile(r"시\s*공\s*(?:이\s*)?가\s*능|기\s*술\s*능\s*력")
# 한국 특허(10-)/실용신안(20-) 등록번호 형식. 전화번호(010-...)·사업자번호에 걸리지 않도록 앞뒤 경계를 둔다.
PATENT_NUMBER = re.compile(r"(?<![\d-])([12]0)\s*-\s*(\d{7})(?!\d)")
# "실적" 단어 없이 "최근 N년간 ... M건 이상" / "M개 단지 이상 ... 공사 완료"로만 표현되는 공고문 대비 폴백 패턴
RECORD_FALLBACK = re.compile(r"최근\s*\d+\s*년간.*?\d+\s*(?:건|회|개\s*단\s*지)\s*이\s*상")
# 참가자격 요건이 아니라 제출서류 목록("실적증명서 1부")의 줄. 요건 줄이 따로 있으면 그쪽을 고른다.
DOCUMENT_LINE = re.compile(r"증명서|사본|협약서|\d+\s*부(?!\S)")
RECENT_YEARS = re.compile(r"최근\s*\d+\s*년")

# 요건이 "없다"고 판단할 때 쓰는 넓은 키워드. 이 중 하나라도 문서에 있으면 "해당 없음"으로 단정하지
# 않는다 (특허 대신 "신기술 지정", "실용신안"으로 요건을 거는 공고 대비).
ABSENCE_CHECK = {
    "capital": CAPITAL_KEYWORD,
    # "최근 1년간 행정처분 확인서"는 대부분의 공고문에 있는 제출서류라 실적 요건의 단서가 아니다
    "record": re.compile(r"실\s*적|최근\s*\d+\s*년간(?!\s*행\s*정\s*처\s*분)"),
    "patent_no": re.compile(
        r"특\s*허|공\s*법|신\s*기\s*술|실\s*용\s*신\s*안|[12]0\s*-\s*\d{7}|시\s*공\s*(?:이\s*)?가\s*능|기\s*술\s*능\s*력"
    ),
}
# K-apt에 세대수가 없는 공고의 보완용 ("단지규모 : 8개동 407세대", "세대수 588세대"). "500세대 이상"은 실적 요건이라 제외.
NOTICE_HOUSEHOLD = re.compile(
    r"(?:단\s*지\s*규\s*모[^\n]{0,40}?|세\s*대\s*수\s*:?\s*)(\d[\d,]*)\s*세\s*대(?!\s*이\s*상)"
)
# PDF 추출 텍스트에 섞이는 폭 없는 문자(예: "5\u2060억")와 HWP에 섞이는 제어 문자. 정규식 매칭을 깨뜨리고
# 제어 문자는 엑셀에 저장할 수 없어(실측: "방수" 검색 공고에서 수집 전체가 오류로 끝남) 제거한다.
INVISIBLE_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200d\u2060\ufeff]")

# "정상"으로 인정할 값의 조건. 키워드가 있는 줄이어도 이 조건을 못 채우면(예: "자본금 증명서 제출",
# "공법 기술사용 협약서") 요건 값이 아니라고 보고 "확인 필요"로 넘긴다 -> "정상" 행은 믿어도 되게 함.
# "3억5천만", "5억", "3천만", "500,000,000원", 쉼표 없는 "300000000원"(7자리 이상, 글자 인식은 쉼표를 자주 놓친다)
CAPITAL_AMOUNT = re.compile(
    r"\d[\d,.]*\s*억(?:\s*\d[\d,.]*\s*(?:천\s*만|백\s*만|만))?"
    r"|\d[\d,.]*\s*(?:천\s*만|백\s*만|만)|\d{1,3}(?:,\d{3})+\s*원|\d{7,}\s*원"
)
UNIT_WON = {"억": 10**8, "천만": 10**7, "백만": 10**6, "만": 10**4}
RECORD_VALUE = re.compile(r"\d+\s*(?:건|회|개(?!\s*년))|\d[\d,.]*\s*(?:억|천\s*만|백\s*만|만)\s*원?")

DOWNLOAD_FAILED_REASON = "공고문을 확보하지 못해 확인 불가"
NOT_FOUND_REASON = "공고문 내용에서 해당 항목 관련 문구를 찾지 못함"
# 관련 문구는 찾았지만 값(금액/건수/특허번호)이 확인되지 않은 경우 셀 앞에 붙는 표시
NEEDS_CHECK_PREFIX = "[확인 필요] "
# shortcut: 실측 스캔 4건 모두 자본금·실적·특허번호가 앞 2쪽 안에 있었음(1쪽에 15~30초), 뒤쪽 요건이 나오면 늘릴 것
OCR_MAX_PAGES = 2
# 스캔본에도 숨은 글자가 몇 자 있을 수 있다 (실측 쪽당 스캔본 5자 이하, 일반 문서 334자 이상)
MIN_PDF_CHARS_PER_PAGE = 100
SCAN_PDF_ERROR = "PDF에서 텍스트를 추출하지 못함(스캔 이미지 PDF일 가능성)"
# 공고문 전체에 관련 문구가 전혀 없으면 요건이 없는 공고로 본다 (실제 공고 25건 중 9건이 특허/공법 요건 없음)
NOT_APPLICABLE = "해당 없음(공고문에 관련 요건 없음)"


@lru_cache
def _ocr_engine():
    # 모델이 크고 스캔 공고에서만 필요해 쓸 때 불러온다
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    # 방향 판별(use_cls)을 켜면 똑바른 줄을 180도 돌려 읽어 "자본금" 줄을 놓친다 (실측 4건 중 2건)
    return RapidOCR(params={
        "Rec.lang_type": LangRec.KOREAN,
        "Rec.ocr_version": OCRVersion.PPOCRV4,
        "Rec.model_type": ModelType.MOBILE,
        "Global.use_cls": False,
    })


def _ocr_pdf(path: str) -> str:
    import pypdfium2

    lines: list[str] = []
    with pypdfium2.PdfDocument(path) as doc:
        for i in range(min(len(doc), OCR_MAX_PAGES)):
            result = _ocr_engine()(doc[i].render(scale=3).to_numpy()[:, :, ::-1])
            lines += result.txts or []
            # 1쪽에 15~30초가 걸려 세 항목을 다 찾으면 다음 쪽은 읽지 않는다
            parsed = parse_requirements("\n".join(lines))
            if all(parsed[f"{key}_found"] for key in FIELDS):
                break
    return "\n".join(lines)


def _cached_ocr_pdf(path: str) -> str:
    """인식한 글자를 PDF 옆에 저장해 두고 재수집 때 다시 쓴다 (스캔 1건에 15~60초).

    공고 저장본(main.CACHE_DIR)을 다시 받을 때 폴더째 지워지므로 원본과 어긋나지 않는다.
    shortcut: OCR_MAX_PAGES나 인식 설정을 바꿔도 저장된 글자를 쓴다, 바꿀 때는 main.CACHE_DIR 버전을 올릴 것
    """
    cache = path + ".ocr.txt"
    try:
        with open(cache, encoding="utf-8") as f:
            return f.read()
    except OSError:
        pass
    text = _ocr_pdf(path)
    if text.strip():
        with open(cache, "w", encoding="utf-8") as f:
            f.write(text)
    return text


def extract_text(path: str, ocr: bool = False) -> tuple[str, str | None]:
    """(추출된 텍스트, 실패 사유) 반환. 성공하면 실패 사유는 None. ocr이면 스캔 PDF를 글자 인식으로 읽는다."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        try:
            with pdfplumber.open(path) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
                min_chars = MIN_PDF_CHARS_PER_PAGE * max(len(pdf.pages), 1)
        except Exception as e:
            return "", f"PDF를 열지 못함({e})"
        if len(text.strip()) >= min_chars:
            return text, None
        if not ocr:
            return "", SCAN_PDF_ERROR
        # 글자 쪽과 스캔 쪽이 섞인 PDF도 있어 이미 읽은 글자에 인식한 글자를 더한다
        try:
            ocr_text = _cached_ocr_pdf(path)
        except Exception as e:
            if not text.strip():
                return "", f"스캔 PDF 글자 인식 실패({str(e)[:100]})"
            ocr_text = ""
        text = f"{text}\n{ocr_text}"
        if not text.strip():
            return "", SCAN_PDF_ERROR
        return text, None

    if ext == ".hwp":
        # hwp5txt는 표(table) 안의 텍스트를 "<표>" placeholder로만 남기고 누락시킨다.
        # 참가자격/실적 요건이 표로 작성된 공고문이 많아, xhtml로 변환 후 HTML을 파싱해
        # 표 내용까지 포함한 전체 텍스트를 얻는다. hwp5html CLI를 파일마다 새 프로세스로 띄우면
        # 파이썬 기동 비용이 커서, 같은 변환을 프로세스 안에서 직접 호출한다 (실측 47개 파일 39초 -> 13초, 결과 동일).
        buf = io.BytesIO()
        try:
            with closing(Hwp5File(path)) as hwp:
                HTMLTransform().transform_hwp5_to_xhtml(hwp, buf)
        except Exception as e:
            return "", f"HWP 파싱 실패({str(e)[:100]})"
        text = BeautifulSoup(buf.getvalue(), "html.parser").get_text("\n")
        if not text.strip():
            return "", "HWP에서 텍스트를 추출하지 못함"
        return text, None

    if ext == ".hwpx":
        try:
            return _hwpx_text(path), None
        except (zipfile.BadZipFile, ElementTree.ParseError, KeyError) as e:
            return "", f"HWPX 파싱 실패({str(e)[:100]})"

    # ponytail: .zip/.doc 등은 미지원 -> 실제로 나오면 추가
    return "", f"지원하지 않는 첨부파일 형식({ext or '확장자 없음'})이라 텍스트 추출 불가"


def _hwpx_text(path: str) -> str:
    """HWPX(zip 안의 Contents/section*.xml)에서 문단(<hp:p>)마다 한 줄씩 텍스트를 뽑는다.
    표 셀 안의 문단도 <hp:p>라서 HWP와 마찬가지로 표 내용이 빠지지 않는다."""
    lines = []
    with zipfile.ZipFile(path) as z:
        sections = sorted(n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n))
        for name in sections:
            parts: list[str] = []
            with z.open(name) as member:
                for _, el in ElementTree.iterparse(member):
                    tag = el.tag.rsplit("}", 1)[-1]
                    if tag == "t":
                        parts.append("".join(el.itertext()))
                    elif tag == "p":
                        lines.append("".join(parts))
                        parts = []
    text = "\n".join(lines)
    if not text.strip():
        raise KeyError("본문 텍스트 없음")
    return text


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
    제출서류 줄("실적증명서 1부")은 값이 있어도 참가자격 요건 줄이 없을 때만 쓴다
    (실측: 래미안금광 공고가 요건 줄보다 제출서류 줄을 먼저 뽑음). 그 경우 엑셀 상태에 의심 사유가 붙는다.
    항목 이름만 나열한 제목 줄(HEADING_LIST)은 후보에서 제외한다.

    continues_from: 키워드가 줄바꿈된 뒷줄에만 있는 경우("라. 최근 5년간 ... 보수공사(" / "실적) 3건 이상")
    바로 앞줄이 이 패턴을 포함하고 현재 줄이 새 번호 항목이 아니면 앞줄을 붙여 요건 문장 전체를 얻는다.
    """
    fallback = document_match = ""
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
        for k, nxt in enumerate([None, *lines[i + 1 : i + 3]]):
            if nxt is not None:
                merged = f"{merged} {nxt}"
            if is_valid(merged):
                # 줄 끝에서 "300세" / "대이상"으로 잘린 세대 조건을 잇는다 (실측: 스캔 PDF 글자 인식 결과)
                rest = lines[i + k + 1 : i + k + 2]
                if rest and re.search(r"\d\s*세$", merged) and rest[0].startswith("대"):
                    merged += rest[0]
                if not is_document_line(merged):
                    return merged, True
                document_match = document_match or merged
                break
        fallback = fallback or _merge_until_digit(lines, i)
    if document_match:
        return document_match, True
    return fallback, False


def is_document_line(sentence: str) -> bool:
    """제출서류 목록에서 뽑힌 문장인지. "이상"이 있으면 요건을 함께 적은 줄로 본다."""
    return bool(DOCUMENT_LINE.search(sentence)) and "이상" not in sentence


def amount_to_won(amount: str) -> int | None:
    """"5억", "3억5천만원", "300,000,000원" -> 원 단위 정수. 해석 못 하면 None."""
    total = 0.0
    for number, unit in re.findall(r"([\d,.]+)(억|천만|백만|만)?", re.sub(r"\s+", "", amount)):
        try:
            total += float(number.replace(",", "")) * UNIT_WON.get(unit, 1)
        except ValueError:
            return None
    return round(total) or None


def normalize_amount(amount: str) -> str:
    """"5 억원", "500,000,000원" -> "5억원(500,000,000원)". 표기가 달라도 같은 금액이면 같은 글자가 된다."""
    won = amount_to_won(amount)
    if won is None:
        return re.sub(r"\s+", "", amount)
    eok, rest = divmod(won, 10**8)
    man, below = divmod(rest, 10**4)
    if below or not (eok or man):
        return f"{won:,}원"
    label = f"{eok}억" if eok else ""
    if man:
        label += f"{man // 1000}천만" if man % 1000 == 0 else f"{man:,}만"
    return f"{label}원({won:,}원)"


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
    joined = "\n".join(lines)
    numbers = dict.fromkeys(f"{a}-{b}" for a, b in PATENT_NUMBER.findall(joined))
    patent_evidence = ""
    if numbers:
        patent_no, patent_ok = ", ".join(numbers), True
        # 번호가 "제10-" / "1234567호"처럼 두 줄에 걸칠 수 있어 번호가 걸친 줄 전체를 근거로 쓴다
        m = PATENT_NUMBER.search(joined)
        end = joined.find("\n", m.end())
        patent_evidence = joined[joined.rfind("\n", 0, m.start()) + 1 : end if end >= 0 else None].replace("\n", " ")
    else:
        # 번호 없이 공법 이름만 있는 경우 -> 해당 줄을 "확인 필요"로 보여준다
        patent_no = next(
            (
                line
                for line in lines
                if (PATENT_KEYWORD.search(line) or METHOD_KEYWORD.search(line) or METHOD_HINT.search(line))
                and not HEADING_LIST.search(line)
            ),
            "",
        )
        patent_ok = False

    # 문서 안의 자본금 금액이 서로 다르면(예: 3억/5억) 어느 쪽이 요건인지 사람이 봐야 한다
    capital_amounts = []
    for line in lines:
        keyword = CAPITAL_KEYWORD.search(line)
        amount = keyword and CAPITAL_AMOUNT.search(line, keyword.end())
        if amount:
            capital_amounts.append(normalize_amount(amount.group()))

    household = NOTICE_HOUSEHOLD.search(text)
    result = {
        "notice_household": household.group(1).replace(",", "") if household else "",
        "capital": capital, "capital_found": capital_ok,
        "record": record, "record_found": record_ok,
        "patent_no": patent_no, "patent_no_found": patent_ok,
        "patent_no_evidence": patent_evidence,
        "capital_amounts": list(dict.fromkeys(capital_amounts)),
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
    # 값을 못 찾은 게 아니라 공고문 자체를 못 읽은 경우 -> "수집 실패"로 분류
    result["extract_failed"] = True
    return result


def parse_files(paths: list[str], fetch_error: str | None = None, ocr: bool = False) -> dict:
    if not paths:
        return _all_missing(fetch_error or DOWNLOAD_FAILED_REASON)

    # 필드별 (값, 조건 만족 여부). 여러 첨부파일 중 조건을 만족하는 값을 우선한다.
    combined: dict[str, tuple[str, bool]] = dict.fromkeys(FIELDS, ("", False))
    sources = dict.fromkeys(FIELDS, "")
    patent_evidence = ""
    notice_household = notice_household_source = ""
    capital_amounts: dict[str, None] = {}
    # 모든 첨부파일에 관련 문구가 전혀 없을 때만 "해당 없음" (추출 실패한 파일이 하나라도 있으면 단정 불가)
    absent = dict.fromkeys(FIELDS, True)
    extract_errors: list[str] = []
    any_text_extracted = False

    for path in paths:
        text, err = extract_text(path, ocr)
        if err:
            extract_errors.append(f"{os.path.basename(path)}: {err}")
            continue
        any_text_extracted = True
        parsed = parse_requirements(text)
        capital_amounts.update(dict.fromkeys(parsed["capital_amounts"]))
        if parsed["notice_household"] and not notice_household:
            notice_household = parsed["notice_household"]
            notice_household_source = os.path.basename(path)
        for key in FIELDS:
            absent[key] = absent[key] and parsed[f"{key}_absent"]
            value, found = parsed[key], parsed[f"{key}_found"]
            cur_value, cur_found = combined[key]
            if value and (not cur_value or (found and not cur_found)):
                combined[key] = (value, found)
                sources[key] = os.path.basename(path)
                if key == "patent_no":
                    patent_evidence = parsed["patent_no_evidence"]

    needs_ocr = any(SCAN_PDF_ERROR in e for e in extract_errors)
    if not any_text_extracted:
        reason = "; ".join(extract_errors) if extract_errors else "첨부파일에서 텍스트를 추출하지 못함"
        return {**_all_missing(reason), "needs_ocr": needs_ocr}

    result = {
        "needs_ocr": needs_ocr,
        "ocr_used": ocr,
        "extract_errors": extract_errors,
        "capital_amounts": list(capital_amounts),
        "patent_no_evidence": patent_evidence,
        "notice_household": notice_household,
        "notice_household_source": notice_household_source,
    }
    for key in FIELDS:
        result[f"{key}_source"] = sources[key]
        value, found = combined[key]
        if found:
            result[key] = value
            result[f"{key}_found"] = True
        elif value:
            result[key] = NEEDS_CHECK_PREFIX + value
            result[f"{key}_found"] = False
        # 글자 인식은 앞쪽만 읽고 오독도 있어 "문구가 없다"고 단정할 수 없다
        elif absent[key] and not extract_errors and not ocr:
            result[key] = NOT_APPLICABLE
            result[f"{key}_found"] = True
        else:
            result[key] = NOT_FOUND_REASON
            result[f"{key}_found"] = False
    return result
