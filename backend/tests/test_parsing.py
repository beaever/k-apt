"""저장된 실제 K-apt 상세페이지 HTML로 파싱 로직 회귀를 방지하는 최소 self-check.

pytest 등 프레임워크 없이 단독 실행 가능: `python tests/test_parsing.py`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile

from pydantic import ValidationError

from app.crawler.step2_download import _parse_detail_html, _unique_path
from app.export.step4_excel import to_row_dict
from app.parser import step3_parse
from app.parser.step3_parse import extract_text, parse_files, parse_requirements

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_a2p_layout_winner():
    """아파트장터(A2P) 형 상세페이지: '낙찰업체 정보' 표에서 회사명을 뽑는다."""
    html = read_fixture("detail_a2p.html")
    detail = _parse_detail_html(html)
    assert detail["winner"] == "주식회사 신광건설", detail
    assert detail["apt_name"] == "광교쌍용포레듀엔1단지", detail
    # K-apt에는 동수 421 / 세대수 7로 뒤바뀌어 등록된 공고 (a2p 원문 기준 세대수 421)
    assert detail["household_count"] == "421", detail


def test_household_count_validation():
    from app.crawler.step2_download import _household_count

    assert _household_count("33", "2328") == "2328"
    assert _household_count("700", "5") == "700"  # 뒤바뀐 값
    assert _household_count("0", "0") == ""  # 미등록 -> 확인 필요


def test_safe_filename_strips_path():
    from app.crawler.step2_download import _safe_filename

    assert _safe_filename("../../etc/공고문.hwp", "x") == "공고문.hwp"
    assert _safe_filename("..\\a\\b.pdf", "x") == "b.pdf"
    assert _safe_filename("", "123.bin") == "123.bin"


def test_kapt_native_layout_winner():
    """K-apt 자체 전자입찰 상세페이지: '참여업체 정보' 표의 successfulY 행에서 회사명을 뽑는다."""
    html = read_fixture("detail_kapt_native.html")
    detail = _parse_detail_html(html)
    assert detail["winner"] == "(주)두루빌엔지니어링", detail
    assert detail["household_count"] == "2328", detail


def test_requirement_line_extraction():
    text = (
        "5. 참가 자격\n"
        "다. 자본금 7억 이상인 업체\n"
        "마. 입찰공고일로부터 최근 5년간 공동주택 해당 면허 관련 공사 5건 이상인 업체\n"
        "다. 아스팔트 보수보강용 유무기 하이브리드 아스콘 코팅제를 이용한 보수공법(특허 제10-2222229호) 협약서 1부\n"
    )
    result = parse_requirements(text)
    assert "자본금 7억 이상" in result["capital"], result
    assert "5년간" in result["record"] and "5건 이상" in result["record"], result
    assert result["patent_no"] == "10-2222229", result


def test_requirement_needs_value_to_count_as_found():
    """키워드만 있고 값(금액/건수/특허번호)이 없는 줄은 "정상"으로 치지 않는다.
    값은 뒤쪽 줄에 있으면 계속 찾아서 쓰고, 끝내 없으면 found=False."""
    text = (
        "4) 자본금 증명서 1부\n"
        "사업실적 : 최근 5년간 공동주택 공사실적 5\n"
        "건 이상인 업체\n"
        "7) 공법 기술사용 협약서\n"
        "(특허 제10-2767595호, 해당 단지명 표기된 공고일 이후 발행분) 1부\n"
        "대표 전화 010-1234567\n"
    )
    result = parse_requirements(text)
    assert result["capital_found"] is False, result
    assert result["record_found"] and "5 건 이상" in result["record"], result
    assert result["patent_no"] == "10-2767595" and result["patent_no_found"], result


def test_absent_requirement_is_not_applicable_but_unextractable_file_is_not():
    """공고문에 관련 문구가 아예 없으면 "해당 없음"(정상), 텍스트를 못 뽑은 파일이 섞이면 단정하지 않는다."""
    with tempfile.TemporaryDirectory() as d:
        doc = os.path.join(d, "a.pdf")
        # 폭 없는 문자(U+2060)가 섞인 PDF 추출 텍스트도 금액으로 인식해야 한다
        text = "자본금 5\u2060억원 이상인 업체\n최근 5년간 공동주택 실적 5건 이상\n"
        import app.parser.step3_parse as p3

        orig = p3.extract_text
        p3.extract_text = lambda path, ocr=False: (text, None) if path == doc else ("", "스캔 PDF")
        try:
            r = p3.parse_files([doc])
            assert r["capital_found"] and "5억원" in r["capital"], r
            assert r["patent_no"] == p3.NOT_APPLICABLE and r["patent_no_found"], r
            r = p3.parse_files([doc, os.path.join(d, "scan.pdf")])
            assert r["patent_no"] == p3.NOT_FOUND_REASON and not r["patent_no_found"], r
        finally:
            p3.extract_text = orig


def test_disciplinary_notice_is_not_a_record_requirement():
    """"최근 1년간 행정처분 확인서"만 있으면 실적 요건이 없는 공고다. 다른 "최근 N년간"은 단정하지 않는다."""
    assert parse_requirements("입찰공고일 전일 기준 최근 1년간\n행정처분 확인서 1부")["record_absent"]
    assert not parse_requirements("최근 3년간 시공 경험이 있는 업체")["record_absent"]


def test_household_from_notice():
    """공고문의 단지규모에서 세대수를 읽고, 실적 요건의 세대수는 무시한다."""
    cases = {
        "다. 단지규모 : 8개동 407세대": "407",
        "라. 단지규모 : 2 개동  103 세대": "103",
        "단지규모 : 2,646세대 28개동": "2646",
        "나. 연면적 및 규모 : 연면적 73,025.014㎡ / 세대수 588세대": "588",
        "최근 5년간 공동주택 500세대 이상 실적 5건": "",
        "세대수 500세대 이상 실적 5건": "",
    }
    for text, expected in cases.items():
        assert parse_requirements(text)["notice_household"] == expected, text


def test_kapt_household_wins_and_notice_value_is_marked():
    """K-apt 값을 우선하고, 공고문 값을 쓸 때만 출처를 근거로 남긴다."""
    base = {"bid_num": "1", "notice_household": "407", "notice_household_source": "공고문.hwp"}
    row = to_row_dict(base)
    assert row["세대수"] == "407" and "공고문.hwp" in row["_근거_세대수"]
    row = to_row_dict({**base, "household_count": "410"})
    assert row["세대수"] == "410" and "_근거_세대수" not in row


def test_ocr_reads_scan_but_row_still_needs_review():
    """스캔 PDF는 글자 인식으로 값을 채우되 정상으로 처리하지 않고, 값이 없어도 해당 없음으로 단정하지 않는다."""
    import pypdfium2

    original = step3_parse._ocr_pdf
    step3_parse._ocr_pdf = lambda path: "3공고일현재자본금5억이상인업체"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scan.pdf")
            doc = pypdfium2.PdfDocument.new()
            doc.new_page(100, 100)
            doc.save(path)
            doc.close()
            assert parse_files([path])["extract_failed"], "OCR을 켜지 않으면 기존처럼 실패"
            parsed = parse_files([path], None, True)
    finally:
        step3_parse._ocr_pdf = original
    assert parsed["capital_found"] and parsed["ocr_used"]
    assert parsed["record"] == step3_parse.NOT_FOUND_REASON
    row = to_row_dict({"bid_num": "1", **parsed})
    assert row["자본금"] == "5억" and "글자 인식" in row["상태"]


def test_ocr_stops_when_all_fields_found_and_limits_pages():
    """세 항목을 다 찾으면 다음 쪽을 읽지 않고, 못 찾아도 최대 쪽수까지만 읽는다."""
    import pypdfium2

    class Engine:
        def __init__(self, text):
            self.text, self.calls = text, 0

        def __call__(self, image):
            self.calls += 1
            return type("Result", (), {"txts": [self.text]})

    original = step3_parse._ocr_engine
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scan.pdf")
            doc = pypdfium2.PdfDocument.new()
            for _ in range(step3_parse.OCR_MAX_PAGES + 1):
                doc.new_page(100, 100)
            doc.save(path)
            doc.close()
            for text, expected in (
                ("자본금5억이상\n실적5건이상\n특허제10-1234567호", 1),
                ("내용없음", step3_parse.OCR_MAX_PAGES),
            ):
                engine = Engine(text)
                step3_parse._ocr_engine = lambda: engine
                step3_parse._ocr_pdf(path)
                assert engine.calls == expected, (text, engine.calls)
    finally:
        step3_parse._ocr_engine = original


def test_hwp_table_content_extracted():
    """실제 K-apt 공고문 hwp: 참가자격(자본금/실적/특허)이 표 안에 들어있는 경우.

    hwp5txt는 표 내용을 "<표>" placeholder로만 남기고 통째로 누락시키는 문제가 있어
    hwp5html로 변환 후 파싱하도록 바꿨다. 표 안의 텍스트가 실제로 뽑히는지 확인.
    """
    path = os.path.join(FIXTURE_DIR, "requirement_in_table.hwp")
    text, err = extract_text(path)
    assert err is None, err
    assert "<표>" not in text, text
    result = parse_requirements(text)
    assert "자본금 5억원 이상" in result["capital"], result
    assert "실적 5건 이상" in result["record"], result
    assert "10-2767595" in result["patent_no"], result


def test_hwpx_text_includes_table_cells():
    """HWPX: zip 안 section XML의 문단/표 셀 텍스트를 줄 단위로 뽑아 요건을 찾는다."""
    import zipfile

    hp = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
    xml = (
        f'<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" {hp}>'
        "<hp:p><hp:run><hp:t>5. 참가자격</hp:t></hp:run></hp:p>"
        "<hp:p><hp:run><hp:tbl><hp:tr><hp:tc><hp:subList>"
        "<hp:p><hp:run><hp:t>다. 자본금 </hp:t><hp:t>5억 이상인 업체</hp:t></hp:run></hp:p>"
        "<hp:p><hp:run><hp:t>라. 최근 5년간 실적 5건 이상</hp:t></hp:run></hp:p>"
        "</hp:subList></hp:tc></hp:tr></hp:tbl></hp:run></hp:p>"
        "</hs:sec>"
    )
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "공고문.hwpx")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("Contents/section0.xml", xml)
        text, err = extract_text(path)
    assert err is None, err
    result = parse_requirements(text)
    assert "자본금 5억 이상" in result["capital"] and result["capital_found"], result
    assert result["record_found"], result


def _complete_row():
    return {
        "success": True,
        "announce_date": "2026-08-01",
        "apt_name": "테스트아파트",
        "work_name": "아스콘 포장공사",
        "household_count": "500",
        "capital": "자본금 5억 이상인 업체",
        "capital_found": True,
        "record": "실적 5건 이상인 업체",
        "record_found": True,
        "patent_no": "10-1234567",
        "patent_no_found": True,
        "bid_method": "최저 낙찰",
        "winner": "테스트건설",
        "bid_amount": "10,000,000",
    }


def test_row_status_normal_when_all_columns_filled():
    row = to_row_dict(_complete_row())
    assert row["상태"] == "정상", row


def test_row_status_fail_when_a_field_is_reason_not_value():
    """크롤링 자체는 성공해도, 자본금 등 값을 못 찾아 사유만 채워졌으면 "실패"로 봐야 한다."""
    row = _complete_row()
    row["capital"] = "공고문 내용에서 해당 항목 관련 문구를 찾지 못함"
    row["capital_found"] = False
    result = to_row_dict(row)
    assert result["상태"].startswith("확인 필요"), result
    assert "자본금" in result["상태"], result


def test_row_status_tiers_and_evidence():
    """공고문을 못 읽으면 "수집 실패", 값이 의심스러우면 "확인 필요", 근거 문장과 파일명은 따로 넘긴다."""
    import app.parser.step3_parse as p3

    row = {**_complete_row(), **p3._all_missing("a.pdf: 스캔 PDF")}
    assert to_row_dict(row)["상태"].startswith("수집 실패"), row

    row = _complete_row()
    row.update(capital_source="공고문.hwp", capital_amounts=["3억", "5억"])
    result = to_row_dict(row)
    assert result["상태"].startswith("확인 필요") and "3억, 5억" in result["상태"], result
    assert result["_근거_자본금"] == "근거: 자본금 5억 이상인 업체\n파일: 공고문.hwp", result

    row = _complete_row()
    row["capital"] = "자본금 500억 이상"
    assert "범위" in to_row_dict(row)["상태"]


def test_row_values_are_summarized():
    """공고일자는 날짜만, 아파트명은 "(입대의)" 제거, 자본금/실적은 핵심 수치만."""
    row = _complete_row()
    row.update(
        announce_date="2026-09-03 16:31:01",
        apt_name="더힐포레(3단지) (입대의)",
        capital="4) 자본금 : 5억원 이상인 업체",
        record="라. 사업실적: 입찰공고일 현재 최근5년간 동일공사 500세대 이상 실적5건 이상인 업체",
    )
    result = to_row_dict(row)
    assert result["공고일자"] == "2026-09-03", result
    assert result["아파트명"] == "더힐포레(3단지)", result
    assert result["자본금"] == "5억", result
    assert result["실적"] == "500세대 5건", result
    row["record"] = "3) 사업실적 : 공고일로부터 최근 5년간 해당 면허 공사 완료 실적 5건 이상인 업체"
    assert to_row_dict(row)["실적"] == "5년 5건"


def test_absent_requirement_labels():
    """요건이 없다고 판정된 칸만 제한없음/미적용으로 보이고, 못 찾은 칸은 사유가 그대로 남는다."""
    from app.parser.step3_parse import NOT_APPLICABLE, NOT_FOUND_REASON

    row = _complete_row()
    row.update(capital=NOT_APPLICABLE, record=NOT_APPLICABLE, patent_no=NOT_APPLICABLE)
    result = to_row_dict(row)
    assert (result["자본금"], result["실적"], result["공법번호"]) == ("제한없음", "제한없음", "미적용"), result
    assert result["상태"] == "정상", result
    row.update(record=NOT_FOUND_REASON, record_found=False)
    result = to_row_dict(row)
    assert result["실적"] == NOT_FOUND_REASON and result["상태"].startswith("확인 필요"), result


def test_unique_path_does_not_overwrite_same_filename():
    with tempfile.TemporaryDirectory() as d:
        first = _unique_path(d, "공고문.hwp")
        open(first, "w").close()
        second = _unique_path(d, "공고문.hwp")
        assert first != second and second.endswith("공고문_1.hwp"), second


def test_collect_request_rejects_empty_or_unknown_regions():
    from app.main import CollectRequest

    base = {"keyword": "아스콘", "date_start": "2026-01-01", "date_end": "2026-01-31"}
    for regions in ([], ["부산"]):
        try:
            CollectRequest(regions=regions, **base)
        except ValidationError:
            continue
        raise AssertionError(f"regions={regions} 가 통과되면 안 됨")
    CollectRequest(regions=["서울"], **base)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"모두 통과 ({len(tests)}개)")
