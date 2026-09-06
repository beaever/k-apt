"""4단계: 수집/파싱 결과를 엑셀로 출력 (10개 컬럼 + 상태)."""

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

COLUMNS = [
    "공고일자", "아파트명", "공사명", "세대수", "자본금",
    "실적", "공법번호", "낙찰방법", "낙찰업체", "낙찰금액", "상태",
]

FAIL_FILL = PatternFill(start_color="FFF8D7DA", end_color="FFF8D7DA", fill_type="solid")
SUCCESS_FONT = Font(color="FF1E7E34", bold=True)
FAIL_FONT = Font(color="FFB02A37", bold=True)


def to_row_dict(r: dict) -> dict:
    """수집 결과 1건을 엑셀 컬럼과 동일한 순서의 dict로 변환.

    엑셀 생성과 API 응답(프런트 결과 테이블)이 같은 컬럼 매핑을 쓰도록 공유.
    상세 크롤링/파싱이 실패한 건도 목록페이지에서 얻은 값(공사명/낙찰방법/낙찰금액 등)은
    그대로 보여주고, 상세페이지 의존 값(세대수/자본금/실적/공법번호/낙찰업체)만 비운다.
    """
    status = "정상" if r.get("success", True) else f"실패: {r.get('error', '알 수 없는 오류')}"
    values = [
        r.get("announce_date", ""),
        r.get("apt_name") or r.get("apt_name_list", ""),
        r.get("work_name") or r.get("bid_title", ""),
        r.get("household_count", ""),
        r.get("capital", ""),
        r.get("record", ""),
        r.get("patent_no", ""),
        r.get("bid_method", ""),
        r.get("winner", ""),
        r.get("bid_amount", ""),
        status,
    ]
    return dict(zip(COLUMNS, values))


def build_excel(rows: list[dict], out_path: str) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "낙찰결과"
    ws.append(COLUMNS)
    status_col = len(COLUMNS)
    for r in rows:
        ws.append(list(to_row_dict(r).values()))
        status_cell = ws.cell(row=ws.max_row, column=status_col)
        if r.get("success", True):
            status_cell.font = SUCCESS_FONT
        else:
            status_cell.font = FAIL_FONT
            for cell in ws[ws.max_row]:
                cell.fill = FAIL_FILL
    wb.save(out_path)
