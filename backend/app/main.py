import asyncio
import os
import uuid
from datetime import date

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel, field_validator

from app.crawler import step1_search, step2_download
from app.export import step4_excel
from app.parser import step3_parse

app = FastAPI(title="K-apt 아스콘 입찰 정보 수집기")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = os.path.join(os.path.dirname(__file__), "storage")
os.makedirs(STORAGE_DIR, exist_ok=True)

DETAIL_CONCURRENCY = 5


class CollectRequest(BaseModel):
    regions: list[str]
    keyword: str
    date_start: date
    date_end: date

    @field_validator("date_end")
    @classmethod
    def _validate_range(cls, date_end, info):
        date_start = info.data.get("date_start")
        if date_start is None:
            return date_end
        if date_end < date_start:
            raise ValueError("종료일이 시작일보다 빠릅니다.")
        if (date_end - date_start).days > 365:
            raise ValueError("검색 기간은 1년 이내여야 합니다.")
        return date_end


async def run_collect(
    regions: list[str], keyword: str, date_start: date, date_end: date, job_dir: str
) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        await step1_search.open_bid_result_page(page)
        await step1_search.search(page, regions, keyword, str(date_start), str(date_end))

        total_pages = await step1_search.get_total_pages(page)
        rows: list[dict] = []
        seen_bid_nums: set[str] = set()
        for page_no in range(1, total_pages + 1):
            if page_no > 1:
                await step1_search.go_to_page(page, page_no)
            for row in await step1_search.scrape_list(page):
                if row["bid_num"] not in seen_bid_nums:
                    seen_bid_nums.add(row["bid_num"])
                    rows.append(row)

        results: list[dict | None] = [None] * len(rows)
        row_queue: asyncio.Queue = asyncio.Queue()
        for i, row in enumerate(rows):
            row_queue.put_nowait((i, row))

        await context.close()

        worker_count = min(DETAIL_CONCURRENCY, len(rows))
        workers = [
            asyncio.create_task(_detail_worker(browser, row_queue, results, job_dir))
            for _ in range(worker_count)
        ]
        await asyncio.gather(*workers)

        await browser.close()
    return results  # type: ignore[return-value]


async def _detail_worker(browser, row_queue: "asyncio.Queue", results: list, job_dir: str):
    """워커마다 독립된 브라우저 컨텍스트(별도 세션/쿠키)를 발급받아 큐를 처리.

    한 세션(컨텍스트)을 여러 탭이 공유하면 동시에 서로 다른 공고의 첨부파일
    목록을 요청할 때 서버 세션 쪽에서 뒤섞여 다운로드가 누락되는 경우가 있어,
    워커별로 완전히 분리된 세션을 쓰도록 함. K-apt에 과도한 동시 요청을 보내지
    않도록 워커 수는 DETAIL_CONCURRENCY로 제한.
    """
    context = await browser.new_context(accept_downloads=True)
    detail_page = await context.new_page()
    try:
        await step1_search.establish_session(detail_page)
        while True:
            try:
                idx, row = row_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                detail = await step2_download.fetch_detail(
                    detail_page, row["bid_num"], row["has_attachment"], job_dir
                )
                parsed = step3_parse.parse_files(
                    detail.get("files", []), detail.get("download_error")
                )
                results[idx] = {**row, **detail, **parsed, "success": True, "error": ""}
            except Exception as e:
                results[idx] = {**row, "success": False, "error": str(e)}
    finally:
        await context.close()


@app.post("/api/collect")
async def collect(req: CollectRequest):
    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(STORAGE_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    results = await run_collect(req.regions, req.keyword, req.date_start, req.date_end, job_dir)
    if not results:
        raise HTTPException(status_code=404, detail="검색 결과가 없습니다.")

    excel_path = os.path.join(job_dir, "result.xlsx")
    step4_excel.build_excel(results, excel_path)

    success_count = sum(1 for r in results if r.get("success", True))
    fail_count = len(results) - success_count

    return {
        "job_id": job_id,
        "row_count": len(results),
        "success_count": success_count,
        "fail_count": fail_count,
        "rows": [step4_excel.to_row_dict(r) for r in results],
    }


@app.get("/api/collect/{job_id}/download")
async def download(job_id: str):
    path = os.path.join(STORAGE_DIR, job_id, "result.xlsx")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="결과 파일을 찾을 수 없습니다.")
    return FileResponse(
        path,
        filename="kapt_asphalt_bid_result.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
