"""2단계: 상세페이지 접속 및 첨부 공고문(HWP/PDF) 다운로드."""

import os

from bs4 import BeautifulSoup

DETAIL_URL_TMPL = "https://www.k-apt.go.kr/bid/bidDetail.do?bidNum={bid_num}&type=3"


def _clean(text: str) -> str:
    return " ".join(text.split())


def _kv_table(table) -> dict:
    """헤더 행(th) + 값 행(td) 1개로 구성된 K-apt 상세 테이블을 dict로 변환.

    일부 테이블은 <thead> 안에 "낙찰업체 정보" 같은 colspan 제목 행이 먼저 오고
    그 다음 행에 실제 컬럼명이 온다 -> thead의 마지막 tr을 헤더로 사용해야 한다.
    """
    thead = table.find("thead")
    if thead:
        header_rows = thead.find_all("tr")
        headers = [_clean(th.get_text()) for th in header_rows[-1].find_all("th")]
        body = table.find("tbody") or table
        data_tr = body.find("tr")
        values = [_clean(td.get_text()) for td in data_tr.find_all("td")] if data_tr else []
        return dict(zip(headers, values))

    trs = table.find_all("tr")
    if len(trs) < 2:
        return {}
    headers = [_clean(th.get_text()) for th in trs[0].find_all("th")]
    values = [_clean(td.get_text()) for td in trs[1].find_all("td")]
    return dict(zip(headers, values))


def _find_winner(soup) -> tuple[str, str]:
    """낙찰업체명/낙찰금액을 찾는다.

    K-apt는 입찰 방식에 따라 상세페이지 레이아웃이 둘로 갈린다:
    1) 외부 대행 시스템(아파트장터 등): "낙찰업체 정보" 표에 낙찰사 1행만 표시 (회사명 헤더)
    2) K-apt 자체 전자입찰: "참여업체 정보" 표에 모든 응찰사가 나열되고, 낙찰사 행에만
       class="successfulY"가 붙는다 (응찰회사 헤더)
    """
    for table in soup.select("table.contTbl"):
        pairs = _kv_table(table)
        if "회사명" in pairs:
            return pairs.get("회사명", ""), pairs.get("계약금액", "")

        win_row = table.select_one("tr[class*=successfulY]")
        if win_row:
            thead = table.find("thead")
            if thead:
                header_rows = thead.find_all("tr")
                headers = [_clean(th.get_text()) for th in header_rows[-1].find_all("th")]
                values = [_clean(td.get_text()) for td in win_row.find_all("td")]
                pairs = dict(zip(headers, values))
                return pairs.get("응찰회사", ""), pairs.get("응찰금액", "")

    return "", ""


def _parse_detail_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    detail = {"apt_name": "", "household_count": "", "winner": "", "work_name": ""}

    for table in soup.select("table.contTbl"):
        pairs = _kv_table(table)
        if "세대수" in pairs and "단지명" in pairs:
            detail["apt_name"] = pairs.get("단지명", "")
            detail["household_count"] = pairs.get("세대수", "")

    detail["winner"], winner_amount = _find_winner(soup)
    if winner_amount:
        detail["winner_amount"] = winner_amount

    label = soup.find("th", string=lambda s: s and "입찰제목" in s)
    if label:
        td = label.find_next_sibling("td")
        detail["work_name"] = _clean(td.get_text()) if td else ""

    return detail


async def _try_download(page, bid_dir: str) -> tuple[list[str], str | None]:
    """"전체 다운로드" 버튼을 한 번 클릭 시도. (파일 목록, 실패 사유) 반환."""
    try:
        # DEXT5 첨부파일 위젯은 networkidle 이후에도 자체 파일목록 AJAX가 끝나야
        # "전체 다운로드" 클릭이 실제로 동작한다. 버튼이 나타날 때까지 대기.
        await page.wait_for_selector("#btn-all-files", state="visible", timeout=15000)
    except Exception:
        return [], "다운로드 버튼이 나타나지 않음(페이지 로딩 지연)"

    downloads: list = []

    def on_download(dl):
        downloads.append(dl)

    page.on("download", on_download)
    try:
        try:
            await page.click("#btn-all-files", timeout=5000)
        except Exception as e:
            return [], f"다운로드 버튼 클릭 실패({e})"

        # 첫 다운로드가 시작될 때까지 최대 30초 폴링, 시작되면 다건 첨부 대비 추가 유예.
        # 동시 여러 탭이 같은 세션으로 요청할 때 서버 응답이 늦어질 수 있어 넉넉하게 대기
        # (실제로는 다운로드가 정상 동작하는데 짧은 타임아웃 탓에 "실패"로 오판되는 경우가 있었음).
        for _ in range(60):
            if downloads:
                break
            await page.wait_for_timeout(500)
        if not downloads:
            return [], "다운로드 버튼 클릭 후 파일 수신 안 됨"
        await page.wait_for_timeout(1500)

        files = []
        for dl in downloads:
            path = os.path.join(bid_dir, dl.suggested_filename)
            await dl.save_as(path)
            files.append(path)
        return files, None
    finally:
        page.remove_listener("download", on_download)


async def _fetch_external_announcement(page, bid_dir: str) -> tuple[list[str], str | None]:
    """K-apt 자체 첨부파일이 없을 때 "해당 공고 가기"로 외부 대행 사이트의 원문을 확보.

    대행사에 따라 두 갈래로 갈린다 (실사이트 확인 결과):
    - kg2b.com 등: 팝업 안에 "공고원문" 링크가 한 번 더 있고, 그 안에 실제 첨부파일(HWP/PDF) 링크가 있음
    - a2p.kr 등: 공고 전체가 페이지에 HTML로 렌더링되어 있고, "공고문 다운로드" 메뉴에서
      그 내용을 PDF로 내려받을 수 있음 (첨부파일은 없지만 원문 PDF는 존재)
    """
    btn = await page.query_selector("text=해당 공고 가기")
    if not btn:
        return [], "첨부파일이 없고 '해당 공고 가기' 버튼도 없어 원문을 확인할 수 없음"

    try:
        async with page.expect_popup(timeout=10000) as popup_info:
            await btn.click()
        popup = await popup_info.value
        await popup.wait_for_load_state("networkidle")
    except Exception as e:
        return [], f"'해당 공고 가기' 클릭 후 원문 페이지가 열리지 않음({e})"

    try:
        doc_link = await popup.query_selector("text=공고원문")
        if doc_link:
            try:
                async with popup.expect_popup(timeout=10000) as doc_popup_info:
                    await doc_link.click()
                doc_popup = await doc_popup_info.value
                await doc_popup.wait_for_load_state("networkidle")
            except Exception as e:
                return [], f"'공고원문' 클릭 후 페이지가 열리지 않음({e})"
            try:
                file_links = await doc_popup.query_selector_all("a[href^='javascript:goLoad']")
                if not file_links:
                    return [], "공고원문 페이지에서 첨부파일 링크를 찾지 못함"
                files = []
                for link in file_links:
                    async with doc_popup.expect_download(timeout=15000) as dl_info:
                        await link.click()
                    dl = await dl_info.value
                    path = os.path.join(bid_dir, dl.suggested_filename)
                    await dl.save_as(path)
                    files.append(path)
                return files, None
            finally:
                await doc_popup.close()

        download_btn = await popup.query_selector("text=공고문 다운로드")
        if download_btn:
            await download_btn.click()
            try:
                async with popup.expect_download(timeout=10000) as dl_info:
                    await popup.click("text=PDF 파일 다운로드")
            except Exception as e:
                return [], f"'공고문 다운로드' 메뉴에서 PDF를 받지 못함({e})"
            dl = await dl_info.value
            path = os.path.join(bid_dir, dl.suggested_filename)
            await dl.save_as(path)
            return [path], None

        return [], "원문 페이지에서 '공고원문' 링크나 '공고문 다운로드' 버튼을 찾지 못함"
    except Exception as e:
        return [], f"외부 원문 사이트에서 첨부파일을 받지 못함({e})"
    finally:
        await popup.close()


async def fetch_detail(page, bid_num: str, has_attachment: bool, download_dir: str) -> dict:
    """상세페이지 접속 -> 아파트명/공사명/세대수/낙찰업체 추출 + 첨부파일(또는 외부 원문) 다운로드."""
    url = DETAIL_URL_TMPL.format(bid_num=bid_num)
    await page.goto(url, wait_until="networkidle")

    html = await page.content()
    detail = _parse_detail_html(html)

    bid_dir = os.path.join(download_dir, bid_num)
    os.makedirs(bid_dir, exist_ok=True)

    if has_attachment:
        files, fetch_error = [], None
        # 동시 여러 탭(worker)이 몰릴 때 DEXT5 다운로드가 이따금 실패하는 경우가 있어
        # 페이지를 새로고침하며 최대 3회까지 재시도. 워커가 각자 큐를 처리하므로
        # 한 건이 재시도로 오래 걸려도 다른 워커 처리량엔 영향 없음.
        for attempt in range(3):
            files, fetch_error = await _try_download(page, bid_dir)
            if files:
                break
            if attempt < 2:
                await page.reload(wait_until="networkidle")
    else:
        files, fetch_error = await _fetch_external_announcement(page, bid_dir)

    detail["files"] = files
    detail["download_error"] = fetch_error
    return detail
