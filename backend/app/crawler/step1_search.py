"""1단계: K-apt 접속 및 낙찰공고 목록 수집."""

BASE_URL = "https://www.k-apt.go.kr"

REGION_CODES = {"서울": "11", "인천": "28", "경기": "41"}


async def establish_session(page):
    """상세페이지 직접 접근에 필요한 세션 쿠키만 가볍게 확보 (메뉴 클릭 없이 메인만 방문)."""
    await page.goto(f"{BASE_URL}/web/main/index.do", wait_until="networkidle")


async def open_bid_result_page(page):
    """메인 페이지 진입 후 메뉴 클릭으로 사업자 선정(경쟁입찰) 결과 공개 페이지 진입.

    직접 URL 접근은 세션 미인증 에러가 발생하므로 반드시 메뉴 클릭 경로를 거친다.
    """
    await page.goto(f"{BASE_URL}/web/main/index.do", wait_until="networkidle")
    # 클릭을 가로채는 공지 레이어 팝업 제거
    await page.evaluate(
        "() => document.querySelectorAll('.layerPopup, .layerPopupClass').forEach(e => e.remove())"
    )
    await page.click("text=입찰정보", force=True)
    await page.wait_for_timeout(500)
    # 모바일 메뉴 등 동일 텍스트의 숨겨진 링크가 같이 잡힐 수 있어 보이는 요소만 선택
    async with page.expect_navigation(wait_until="networkidle"):
        await page.click("a[href='/bid/bidList.do?type=3']:visible", force=True)
    # 페이지 전환 직후 inline script(setArea/initList 등) 파싱이 늦을 때가 있어 대기
    await page.wait_for_function("typeof setArea === 'function'")


async def search(page, regions: list[str], keyword: str, date_start: str, date_end: str):
    """지역/검색어/기간(직접 지정한 시작일~종료일) 필터를 적용하고 낙찰공고 목록을 검색."""
    region_codes = [REGION_CODES[r] for r in regions if r in REGION_CODES]

    await page.evaluate(
        """(args) => {
            args.codes.forEach(c => setArea(c));
            document.getElementById('bid_state').value = '5'; // 낙찰공고
            document.getElementById('bidTitle').value = args.keyword;
            document.getElementsByName('dateStart')[0].value = args.dateStart;
            document.getElementsByName('dateEnd')[0].value = args.dateEnd;
        }""",
        {
            "codes": region_codes,
            "keyword": keyword,
            "dateStart": date_start,
            "dateEnd": date_end,
        },
    )
    async with page.expect_navigation(wait_until="networkidle"):
        await page.evaluate("initList(3, '');")
    # 페이지당 결과 수를 최대(100건)로 올려서 순회할 페이지 수를 줄인다
    async with page.expect_navigation(wait_until="networkidle"):
        await page.evaluate(
            "() => { document.getElementById('pageSelect').value = '100'; initList(3, ''); }"
        )


async def get_total_pages(page) -> int:
    """페이징 영역에서 마지막 페이지 번호를 읽는다. 페이지가 1개뿐이면 1을 반환."""
    return await page.evaluate(
        """() => {
            const last = document.querySelector('.control a.last');
            if (last) {
                const m = last.getAttribute('href').match(/goList\\((\\d+)\\)/);
                if (m) return parseInt(m[1], 10);
            }
            const pages = Array.from(document.querySelectorAll('.pages a.page'))
                .map(a => parseInt(a.textContent, 10))
                .filter(n => !Number.isNaN(n));
            return pages.length ? Math.max(...pages) : 1;
        }"""
    )


async def go_to_page(page, page_no: int):
    async with page.expect_navigation(wait_until="networkidle"):
        await page.evaluate("(n) => goList(n)", page_no)


async def scrape_list(page) -> list[dict]:
    """결과 테이블에서 기본 정보(공고번호, 낙찰방법, 낙찰금액, 공고일 등)를 추출."""
    return await page.evaluate(
        """() => {
            const rows = document.querySelectorAll('#tblBidList tbody tr');
            return Array.from(rows).map(tr => {
                const tds = tr.querySelectorAll('td');
                if (tds.length < 9) return null;
                return {
                    bid_num: tr.getAttribute('dataid'),
                    bid_method: tds[2].innerText.trim(),
                    bid_title: tds[3].innerText.trim(),
                    has_attachment: !!tds[3].querySelector('img[alt="첨부파일있음"]'),
                    bid_amount: tds[6].innerText.trim(),
                    apt_name_list: tds[7].innerText.trim(),
                    announce_date: tds[8].innerText.trim(),
                };
            }).filter(Boolean);
        }"""
    )
