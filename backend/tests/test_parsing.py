"""저장된 실제 K-apt 상세페이지 HTML로 파싱 로직 회귀를 방지하는 최소 self-check.

pytest 등 프레임워크 없이 단독 실행 가능: `python tests/test_parsing.py`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.crawler.step2_download import _parse_detail_html
from app.parser.step3_parse import parse_requirements

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
    assert detail["household_count"] == "7", detail


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


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"모두 통과 ({len(tests)}개)")
