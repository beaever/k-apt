"""2단계: 상세페이지 접속 및 첨부 공고문(HWP/PDF) 다운로드."""

import json
import os

from bs4 import BeautifulSoup

DETAIL_URL_TMPL = "https://www.k-apt.go.kr/bid/bidDetail.do?bidNum={bid_num}&type=3"
# 상세페이지 첨부 위젯(DEXT5)이 내부적으로 호출하는 첨부 목록/다운로드 주소 (실사이트 네트워크 요청으로 확인)
FILE_LIST_URL = "https://www.k-apt.go.kr/bid/bidFileListData.do?seq=BID_FILE"
FILE_DOWNLOAD_URL = "https://www.k-apt.go.kr/cmm/file/BID/fileDownload.do"


def _clean(text: str) -> str:
    return " ".join(text.split())


def _unique_path(dir_path: str, filename: str) -> str:
    """같은 공고에 이름이 같은 첨부파일이 여러 개면 덮어쓰지 않도록 _1, _2 접미사를 붙인다."""
    path = os.path.join(dir_path, filename)
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{stem}_{n}{ext}"
        n += 1
    return path


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


def _household_count(buildings: str, households: str) -> str:
    """K-apt에 등록된 동수/세대수를 검증해 세대수를 돌려준다.

    외부 대행사 공고는 등록 단계에서 값이 틀어진 경우가 많다 (실측 13건 중 6건):
    - 동수·세대수가 모두 0 -> 모르는 값이므로 빈 칸 (상태가 "확인 필요"로 표시됨)
    - 동수 > 세대수 -> 한 동에 1세대 이상이므로 불가능, 두 값이 뒤바뀐 것 (a2p 원문과 대조해 확인)
    """
    b, h = (int(v.replace(",", "")) if v.replace(",", "").isdigit() else 0 for v in (buildings, households))
    if b > h > 0:
        return str(b)
    return str(h) if h > 0 else ""


def _parse_detail_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    detail = {"apt_name": "", "household_count": "", "winner": "", "work_name": ""}

    for table in soup.select("table.contTbl"):
        pairs = _kv_table(table)
        if "세대수" in pairs and "단지명" in pairs:
            detail["apt_name"] = pairs.get("단지명", "")
            detail["household_count"] = _household_count(pairs.get("동수", ""), pairs.get("세대수", ""))

    detail["winner"], winner_amount = _find_winner(soup)
    if winner_amount:
        detail["winner_amount"] = winner_amount

    label = soup.find("th", string=lambda s: s and "입찰제목" in s)
    if label:
        td = label.find_next_sibling("td")
        detail["work_name"] = _clean(td.get_text()) if td else ""

    return detail


def _safe_filename(name: str, fallback: str) -> str:
    """서버가 준 파일명에서 경로 성분을 제거 (예: "../x.hwp"가 다운로드 폴더 밖에 저장되지 않도록)."""
    return os.path.basename(name.replace("\\", "/")).strip() or fallback


async def _download_kapt_files(request, html: str, bid_num: str, bid_dir: str) -> tuple[list[str], str | None]:
    """K-apt 자체 첨부파일을 브라우저 위젯 없이 HTTP로 받는다. (파일 목록, 실패 사유) 반환.

    예전에는 DEXT5 위젯의 "전체 다운로드"를 누르고 첫 파일 이후 1.5초만 기다렸는데, 첨부가 여러 개면
    뒤쪽 파일이 늦게 와서 조용히 빠질 수 있었다(실측: 3개 중 마지막이 1.1초 뒤 도착). 위젯이 내부적으로
    쓰는 목록 API로 받을 파일을 먼저 확정하고 하나씩 받아, 개수가 맞지 않으면 실패로 남긴다.
    """
    token = BeautifulSoup(html, "html.parser").select_one("meta[name=_csrf]")
    if not token:
        return [], "상세페이지에서 보안 토큰을 찾지 못해 첨부 목록을 조회할 수 없음"
    resp = await request.post(
        FILE_LIST_URL,
        data=json.dumps({"bidNum": bid_num}),
        headers={
            "X-CSRF-TOKEN": token["content"],
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json;charset=UTF-8",
            # 없으면 같은 내용을 XML로 돌려준다
            "Accept": "application/json",
        },
    )
    try:
        body = await resp.json()
    except Exception:
        return [], f"첨부 목록 조회 실패(HTTP {resp.status}, JSON 아님)"
    if body.get("code") != "SCC":
        return [], f"첨부 목록 조회 실패({body.get('msg') or body.get('code')})"
    entries = body.get("data") or []
    if not entries:
        return [], "목록에는 첨부 표시가 있지만 첨부파일 목록이 비어 있음"

    files, errors = [], []
    for entry in entries:
        name = _safe_filename(entry.get("fileName", ""), f"{entry.get('seq')}.bin")
        body = None
        for _ in range(3):
            r = await request.get(FILE_DOWNLOAD_URL, params={"key": str(entry["seq"]), "fileName": name})
            if r.ok and (body := await r.body()):
                break
            body = None
        if body is None:
            errors.append(name)
            continue
        path = _unique_path(bid_dir, name)
        with open(path, "wb") as f:
            f.write(body)
        files.append(path)
    if errors:
        # 일부만 받은 상태로 "해당 없음"이 판정되면 안 되므로 실패 사유를 함께 넘긴다
        return files, f"첨부파일 {len(entries)}개 중 {len(errors)}개를 받지 못함({', '.join(errors)})"
    return files, None


def _household_from_external(html: str) -> str:
    """a2p.kr 원문 페이지의 단지 정보 표에서 세대수를 읽는다 (K-apt 쪽 값이 0이거나 뒤바뀐 경우 보정용)."""
    for table in BeautifulSoup(html, "html.parser").find_all("table"):
        value = _kv_table(table).get("세대수", "").replace(",", "")
        if value.isdigit() and int(value) > 0:
            return value
    return ""


async def _fetch_external_announcement(page, bid_dir: str, detail: dict) -> tuple[list[str], str | None]:
    """K-apt 자체 첨부파일이 없을 때 "해당 공고 가기"로 외부 대행 사이트의 원문을 확보.

    대행사에 따라 두 갈래로 갈린다 (실사이트 확인 결과):
    - kg2b.com 등: 팝업 안에 "공고원문" 링크가 한 번 더 있고, 그 안에 실제 첨부파일(HWP/PDF) 링크가 있음
    - a2p.kr 등: 공고 전체가 페이지에 HTML로 렌더링되어 있고, "공고문 (전체) 다운로드" 메뉴에서
      그 내용을 PDF로 내려받을 수 있음 (첨부파일은 없지만 원문 PDF는 존재)

    페이지 로딩은 networkidle(모든 요청이 끝날 때까지) 대신 필요한 링크/버튼이 보이는 즉시 진행한다.
    a2p 원문에 세대수가 있으면 detail["household_count"]를 그 값으로 보정한다.
    """
    btn = page.locator("text=해당 공고 가기").first
    try:
        await btn.wait_for(timeout=20000)
    except Exception:
        return [], "첨부파일이 없고 '해당 공고 가기' 버튼도 없어 원문을 확인할 수 없음"

    try:
        # 대행사 사이트가 느려 10초 안에 안 뜨는 경우가 있었음 (다시 열면 정상) -> 넉넉히 대기
        async with page.expect_popup(timeout=30000) as popup_info:
            await btn.click()
        popup = await popup_info.value
    except Exception as e:
        return [], f"'해당 공고 가기' 클릭 후 원문 페이지가 열리지 않음({e})"

    try:
        doc_link = popup.locator("text=공고원문").first
        # a2p.kr은 2026년 8월경 버튼 이름을 "공고문 다운로드" -> "공고문 전체 다운로드"로 바꿨다
        download_btn = popup.locator("text=/공고문\\s*(전체\\s*)?다운로드/").first
        try:
            await doc_link.or_(download_btn).first.wait_for(timeout=30000)
        except Exception:
            return [], "원문 페이지에서 '공고원문' 링크나 '공고문 다운로드' 버튼을 찾지 못함"

        if await doc_link.count():
            try:
                async with popup.expect_popup(timeout=30000) as doc_popup_info:
                    await doc_link.click()
                doc_popup = await doc_popup_info.value
            except Exception as e:
                return [], f"'공고원문' 클릭 후 페이지가 열리지 않음({e})"
            try:
                try:
                    await doc_popup.locator("a[href^='javascript:goLoad']").first.wait_for(timeout=15000)
                except Exception:
                    pass
                file_links = await doc_popup.query_selector_all("a[href^='javascript:goLoad']")
                if not file_links:
                    return [], "공고원문 페이지에서 첨부파일 링크를 찾지 못함"
                files = []
                for link in file_links:
                    async with doc_popup.expect_download(timeout=15000) as dl_info:
                        await link.click()
                    dl = await dl_info.value
                    path = _unique_path(bid_dir, dl.suggested_filename)
                    await dl.save_as(path)
                    files.append(path)
                return files, None
            finally:
                await doc_popup.close()

        if await download_btn.count():
            if household := _household_from_external(await popup.content()):
                detail["household_count"] = household
            await download_btn.click()
            try:
                async with popup.expect_download(timeout=10000) as dl_info:
                    await popup.click("text=PDF 파일 다운로드")
            except Exception as e:
                return [], f"'공고문 다운로드' 메뉴에서 PDF를 받지 못함({e})"
            dl = await dl_info.value
            path = _unique_path(bid_dir, dl.suggested_filename)
            await dl.save_as(path)
            return [path], None

        return [], "원문 페이지에서 '공고원문' 링크나 '공고문 다운로드' 버튼을 찾지 못함"
    except Exception as e:
        return [], f"외부 원문 사이트에서 첨부파일을 받지 못함({e})"
    finally:
        await popup.close()


async def fetch_detail(page, bid_num: str, has_attachment: bool, download_dir: str) -> dict:
    """상세페이지 -> 아파트명/공사명/세대수/낙찰업체 추출 + 첨부파일(또는 외부 원문) 다운로드.

    K-apt 상세페이지와 자체 첨부파일은 브라우저 렌더링 없이 같은 세션의 HTTP 요청으로 처리한다
    (실측: 브라우저 3.1초+위젯 3.0초 -> HTTP 0.5초 수준). 외부 대행사 원문은 팝업을 거쳐야 해서 브라우저를 쓴다.
    """
    url = DETAIL_URL_TMPL.format(bid_num=bid_num)
    request = page.context.request
    resp = await request.get(url)
    if not resp.ok:
        raise RuntimeError(f"상세페이지 응답 오류(HTTP {resp.status})")
    html = await resp.text()
    detail = _parse_detail_html(html)

    bid_dir = os.path.join(download_dir, bid_num)
    os.makedirs(bid_dir, exist_ok=True)

    if has_attachment:
        files, fetch_error = await _download_kapt_files(request, html, bid_num, bid_dir)
    else:
        await page.goto(url, wait_until="domcontentloaded")
        files, fetch_error = await _fetch_external_announcement(page, bid_dir, detail)

    detail["files"] = files
    detail["download_error"] = fetch_error
    return detail
