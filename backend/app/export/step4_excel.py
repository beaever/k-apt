"""4단계: 수집/파싱 결과를 엑셀로 출력 (10개 컬럼 + 상태).

"상태"는 크롤링 도중 예외가 났는지만으로 판단하지 않는다. 10개 데이터 컬럼이
전부 실제 값으로 채워져 있어야 "정상"이고, 자본금/실적/공법번호 중 하나라도
값을 못 찾아 사유 문구만 채워졌거나(= parse_files의 *_found 플래그가 False),
그 외 컬럼이 비어 있으면 "실패"로 본다.
"""

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from app.crawler.step2_download import DETAIL_URL_TMPL

COLUMNS = [
    "공고일자", "아파트명", "공사명", "세대수", "자본금",
    "실적", "공법번호", "낙찰방법", "낙찰업체", "낙찰금액", "공고 링크", "상태",
]

# 값 대신 "사유"가 들어갈 수 있는 컬럼 -> parse_files()가 반환하는 "실제로 찾았는지" 플래그 키
FOUND_FLAG_KEYS = {"자본금": "capital_found", "실적": "record_found", "공법번호": "patent_no_found"}

FAIL_FILL = PatternFill(start_color="FFF8D7DA", end_color="FFF8D7DA", fill_type="solid")
SUCCESS_FONT = Font(color="FF1E7E34", bold=True)
FAIL_FONT = Font(color="FFB02A37", bold=True)


def to_row_dict(r: dict) -> dict:
    """수집 결과 1건을 엑셀 컬럼과 동일한 순서의 dict로 변환.

    엑셀 생성과 API 응답(프런트 결과 테이블)이 같은 컬럼 매핑을 쓰도록 공유.
    """
    values = {
        "공고일자": r.get("announce_date", ""),
        "아파트명": r.get("apt_name") or r.get("apt_name_list", ""),
        "공사명": r.get("work_name") or r.get("bid_title", ""),
        "세대수": r.get("household_count", ""),
        "자본금": r.get("capital", ""),
        "실적": r.get("record", ""),
        "공법번호": r.get("patent_no", ""),
        "낙찰방법": r.get("bid_method", ""),
        "낙찰업체": r.get("winner", ""),
        "낙찰금액": r.get("bid_amount") or r.get("winner_amount", ""),
    }

    if not r.get("success", True):
        status = f"실패: {r.get('error', '알 수 없는 오류')}"
    else:
        missing = []
        for col, value in values.items():
            found_flag = FOUND_FLAG_KEYS.get(col)
            if found_flag is not None:
                if not r.get(found_flag, bool(value)):
                    missing.append(f"{col}({value or '값 없음'})")
            elif not value:
                missing.append(f"{col} 정보 없음")
        status = ("실패: " + " / ".join(missing)) if missing else "정상"

    bid_num = r.get("bid_num")
    values["공고 링크"] = DETAIL_URL_TMPL.format(bid_num=bid_num) if bid_num else ""
    values["상태"] = status
    return values


def build_excel(row_dicts: list[dict], out_path: str) -> None:
    """to_row_dict()로 이미 변환된 행(dict)들을 받아 엑셀로 저장."""
    wb = Workbook()
    ws = wb.active
    ws.title = "낙찰결과"
    ws.append(COLUMNS)
    status_col = len(COLUMNS)
    for row_dict in row_dicts:
        ws.append(list(row_dict.values()))
        status_cell = ws.cell(row=ws.max_row, column=status_col)
        if row_dict["상태"] == "정상":
            status_cell.font = SUCCESS_FONT
        else:
            status_cell.font = FAIL_FONT
            for cell in ws[ws.max_row]:
                cell.fill = FAIL_FILL
    wb.save(out_path)
