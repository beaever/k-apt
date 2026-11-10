import asyncio
import json
import os
import re
import shutil
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from multiprocessing import get_context

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from playwright.async_api import async_playwright
from pydantic import BaseModel, field_validator

from app.crawler import step1_search, step2_download
from app.export import step4_excel
from app.parser import step3_parse

app = FastAPI(title="K-apt 낙찰 정보 수집기")

# 배포 시 CORS_ORIGINS 환경변수(쉼표 구분)로 프런트엔드 주소를 제한할 수 있음.
# 미설정 시 기존과 동일하게 전체 허용(로컬 개발용 기본값).
_cors_origins_env = os.environ.get("CORS_ORIGINS")
_cors_origins = [o.strip() for o in _cors_origins_env.split(",")] if _cors_origins_env else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = os.path.join(os.path.dirname(__file__), "storage")
os.makedirs(STORAGE_DIR, exist_ok=True)
# 이 시간보다 오래된 작업 폴더(다운로드 첨부파일 + 엑셀)는 새 작업 시작 시 삭제
STORAGE_TTL_SEC = 24 * 3600
# 공고별 상세정보·첨부파일 저장본. 같은 공고를 다시 수집할 때 K-apt에 접속하지 않고 재사용한다.
# 분석(PDF/HWP 읽기·판정)은 매번 새로 하므로 분석 규칙을 고치면 저장본에도 바로 반영된다.
# step2_download의 상세정보 추출 방식을 바꾸면 이름의 버전을 올려 옛 저장본을 버린다.
CACHE_DIR = os.path.join(STORAGE_DIR, "cache-v1")
# shortcut: 공고가 정정돼도 이 기간에는 저장본을 쓴다, 정정이 잦으면 기간을 줄일 것
CACHE_TTL_SEC = 30 * 24 * 3600
CACHE_DETAIL_FILE = "detail.json"

# 무료 호스팅은 메모리가 적어 동시 크로미움 컨텍스트 수를 줄여야 할 수 있음.
DETAIL_CONCURRENCY = int(os.environ.get("DETAIL_CONCURRENCY", "5"))

FETCH_ATTEMPTS = 3

# PDF/HWP 파싱은 순수 파이썬 CPU 작업이라 스레드로 돌리면 GIL을 잡고 있어 크롤링(이벤트 루프)까지 느려진다
# (실측: 공고 1건 처리가 단독 0.3초 -> 파싱과 동시 실행 시 수 초). 별도 프로세스에서 돌린다.
# 무료 호스팅은 메모리가 적어 PARSE_PROCESSES로 줄일 수 있음 (프로세스당 약 100MB).
PARSE_PROCESSES = int(os.environ.get("PARSE_PROCESSES", str(min(4, os.cpu_count() or 1))))
_parse_pool: ProcessPoolExecutor | None = None
# 글자 인식은 약 900MB를 쓰므로 동시에 하나만 돌리고 끝나면 프로세스째 메모리를 돌려준다.
# 메모리가 적은 호스팅은 OCR_ENABLED=0으로 끈다 (스캔 PDF는 "수집 실패"로 남음).
OCR_ENABLED = os.environ.get("OCR_ENABLED", "1") == "1"
_ocr_pool: ProcessPoolExecutor | None = None


def _get_parse_pool() -> ProcessPoolExecutor:
    global _parse_pool
    if _parse_pool is None:
        # Linux 기본값 fork는 Playwright 연결을 물려받아 수집 종료 단계에서 멈춘다
        _parse_pool = ProcessPoolExecutor(max_workers=PARSE_PROCESSES, mp_context=get_context("spawn"))
    return _parse_pool


def _get_ocr_pool() -> ProcessPoolExecutor:
    global _ocr_pool
    if _ocr_pool is None:
        _ocr_pool = ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"), max_tasks_per_child=1)
    return _ocr_pool

# 수집은 수 분~수십 분 걸려 HTTP 요청 하나로 기다리면 브라우저/프록시가 먼저 끊는다.
# 백그라운드 작업으로 돌리고 프런트는 상태를 폴링한다.
# ponytail: 프로세스 메모리 보관 -> 서버 재시작 시 진행 상태 유실, 인스턴스 여러 대면 외부 저장소 필요
JOBS: dict[str, dict] = {}
_background_tasks: set[asyncio.Task] = set()


class CollectRequest(BaseModel):
    regions: list[str]
    keyword: str
    date_start: date
    date_end: date

    @field_validator("regions")
    @classmethod
    def _validate_regions(cls, regions):
        if not regions:
            raise ValueError("지역을 1개 이상 선택해야 합니다.")
        unknown = [r for r in regions if r not in step1_search.REGION_CODES]
        if unknown:
            raise ValueError(f"지원하지 않는 지역입니다: {', '.join(unknown)}")
        return regions

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
    regions: list[str], keyword: str, date_start: date, date_end: date, job: dict
) -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        job["message"] = "K-apt에 접속해 조건에 맞는 낙찰공고 목록을 검색하고 있어요"
        await step1_search.open_bid_result_page(page)
        await step1_search.search(page, regions, keyword, str(date_start), str(date_end))

        total_pages = await step1_search.get_total_pages(page)
        rows: list[dict] = []
        seen_bid_nums: set[str] = set()
        for page_no in range(1, total_pages + 1):
            job["message"] = f"낙찰공고 목록을 수집하고 있어요 ({page_no}/{total_pages} 페이지)"
            if page_no > 1:
                await step1_search.go_to_page(page, page_no)
            for row in await step1_search.scrape_list(page):
                # 공고번호가 없는 행은 서로 다른 공고일 수 있어 합치지 않는다 (워커가 실패로 남김)
                if not _valid_bid_num(row["bid_num"]) or row["bid_num"] not in seen_bid_nums:
                    seen_bid_nums.add(row["bid_num"])
                    rows.append(row)

        results: list[dict | None] = [None] * len(rows)
        row_queue: asyncio.Queue = asyncio.Queue()
        for i, row in enumerate(rows):
            row_queue.put_nowait((i, row))

        await context.close()

        job["total"] = len(rows)
        job["message"] = "공고별 상세정보와 입찰공고문(HWP/PDF)을 처리하고 있어요"
        worker_count = min(DETAIL_CONCURRENCY, len(rows))
        workers = [
            asyncio.create_task(_detail_worker(browser, row_queue, results, job))
            for _ in range(worker_count)
        ]
        # 워커 하나가 세션 확보 등에서 죽어도 나머지 워커가 큐를 계속 처리하도록 예외를 전파하지 않음
        await asyncio.gather(*workers, return_exceptions=True)

        await browser.close()
    if "blocked" in job:
        raise step1_search.KaptAccessBlocked(job["blocked"])
    # 모든 워커가 죽어 처리되지 못한 행은 None으로 남는다 -> 실패 행으로 채워 목록에서 누락되지 않게 함
    return [
        r or {**row, "success": False, "error": "처리되지 않음(브라우저 세션 확보 실패)"}
        for r, row in zip(results, rows)
    ]


async def _detail_worker(browser, row_queue: "asyncio.Queue", results: list, job: dict):
    """워커마다 독립된 브라우저 컨텍스트(별도 세션/쿠키)를 발급받아 큐를 처리.

    K-apt 상세/첨부는 컨텍스트의 HTTP 요청으로, 외부 대행사 원문은 같은 컨텍스트의 브라우저 탭으로
    처리한다. 서버 세션에 공고별 상태(첨부 위젯 등)가 섞이지 않도록 워커별로 분리된 세션을 쓴다. K-apt에 과도한 동시 요청을 보내지
    않도록 워커 수는 DETAIL_CONCURRENCY로 제한.
    """
    context = await browser.new_context(accept_downloads=True)
    detail_page = await context.new_page()
    # 파싱(PDF/HWP)은 스레드에서 돌리고 기다리지 않은 채 다음 공고 다운로드로 넘어간다
    # -> 네트워크 대기와 파싱이 겹쳐 워커당 공고 1건 처리 시간이 줄어든다.
    parse_tasks: list[asyncio.Task] = []
    try:
        await step1_search.establish_session(detail_page)
        while "blocked" not in job:
            try:
                idx, row = row_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            # 공고번호는 폴더 이름과 삭제 경로에 쓰인다 -> 빈 값이면 저장본 폴더 전체가 지워질 수 있다
            if not _valid_bid_num(row["bid_num"]):
                results[idx] = {**row, "success": False, "error": "목록에서 공고번호를 읽지 못함(K-apt 화면이 바뀌었을 수 있음)"}
                job["done"] += 1
                continue
            if detail := _load_cached_detail(row["bid_num"]):
                parse_tasks.append(asyncio.create_task(_parse_and_store(idx, row, detail, results, job)))
                continue
            detail = None
            for attempt in range(FETCH_ATTEMPTS):
                # 실패한 시도가 남긴 파일이 저장본에 섞이지 않도록 매번 비우고 받는다
                shutil.rmtree(os.path.join(CACHE_DIR, row["bid_num"]), ignore_errors=True)
                try:
                    detail = await step2_download.fetch_detail(
                        detail_page, row["bid_num"], row["has_attachment"], CACHE_DIR
                    )
                    # 외부 원문 사이트 지연은 예외가 아니라 빈 파일 목록과 사유로 돌아온다
                    if detail["files"] or detail["download_error"] in step2_download.PERMANENT_ERRORS:
                        break
                except step1_search.KaptAccessBlocked as e:
                    job["blocked"] = str(e)
                    break
                except Exception as e:
                    results[idx] = {**row, "success": False, "error": str(e)}
                # 타임아웃 난 페이지 이동이 뒤늦게 끝나면서 다음 공고의 이동을 끊는 경우가 있어
                # ("interrupted by another navigation") 페이지를 새로 만들어 다시 시도한다.
                await detail_page.close()
                detail_page = await context.new_page()
            if "blocked" in job:
                break
            if detail is None:
                job["done"] += 1
                continue
            if detail["files"] and not detail["download_error"]:
                _save_cached_detail(row["bid_num"], detail)
            parse_tasks.append(asyncio.create_task(_parse_and_store(idx, row, detail, results, job)))
    finally:
        await context.close()
        await asyncio.gather(*parse_tasks)


async def _parse_and_store(idx: int, row: dict, detail: dict, results: list, job: dict):
    try:
        loop = asyncio.get_running_loop()
        files, fetch_error = detail.get("files", []), detail.get("download_error")
        parsed = await loop.run_in_executor(_get_parse_pool(), step3_parse.parse_files, files, fetch_error)
        if OCR_ENABLED and parsed.get("needs_ocr"):
            parsed = await loop.run_in_executor(_get_ocr_pool(), step3_parse.parse_files, files, fetch_error, True)
        results[idx] = {**row, **detail, **parsed, "success": True, "error": ""}
    except Exception as e:
        results[idx] = {**row, **detail, "success": False, "error": f"공고문 분석 중 오류({e})"}
    job["done"] += 1


def _cleanup_old_jobs():
    cutoff = time.time() - STORAGE_TTL_SEC
    for name in os.listdir(STORAGE_DIR):
        path = os.path.join(STORAGE_DIR, name)
        if name.startswith("cache-"):
            # 버전이 바뀐 옛 저장본은 통째로 지운다
            if path != CACHE_DIR:
                shutil.rmtree(path, ignore_errors=True)
            continue
        if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            JOBS.pop(name, None)
    cache_cutoff = time.time() - CACHE_TTL_SEC
    for name in os.listdir(CACHE_DIR) if os.path.isdir(CACHE_DIR) else []:
        path = os.path.join(CACHE_DIR, name)
        if os.path.getmtime(path) < cache_cutoff:
            shutil.rmtree(path, ignore_errors=True)


def _valid_bid_num(bid_num) -> bool:
    """K-apt 공고번호("20261001150247153")와 외부 대행사 번호("kg2b_141845") 형식만 허용."""
    return isinstance(bid_num, str) and re.fullmatch(r"[\w-]+", bid_num, re.ASCII) is not None


def _load_cached_detail(bid_num: str) -> dict | None:
    entry = os.path.join(CACHE_DIR, bid_num)
    try:
        with open(os.path.join(entry, CACHE_DETAIL_FILE), encoding="utf-8") as f:
            detail = json.load(f)
    except (OSError, ValueError):
        return None
    detail["files"] = [os.path.join(entry, name) for name in detail["files"]]
    if not all(os.path.exists(path) for path in detail["files"]):
        return None
    return detail


def _save_cached_detail(bid_num: str, detail: dict):
    """첨부를 빠짐없이 받은 공고만 저장한다 (일부만 받았거나 실패한 공고는 다음 수집 때 다시 받음)."""
    entry = os.path.join(CACHE_DIR, bid_num)
    data = {**detail, "files": [os.path.relpath(path, entry) for path in detail["files"]]}
    with open(os.path.join(entry, CACHE_DETAIL_FILE), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


async def _run_job(job_id: str, req: CollectRequest, job_dir: str):
    job = JOBS[job_id]
    try:
        results = await run_collect(
            req.regions, req.keyword, req.date_start, req.date_end, job
        )
        if not results:
            job.update(status="error", message="검색 결과가 없습니다.")
            return

        # 표시용 공고일자는 날짜만 남으므로, 같은 날짜도 시각순이 되도록 변환 전 원본 일시로 정렬
        results.sort(key=lambda r: r.get("announce_date", ""))
        row_dicts = [step4_excel.to_row_dict(r) for r in results]
        step4_excel.build_excel(row_dicts, os.path.join(job_dir, "result.xlsx"))
        job.update(
            status="done",
            rows=row_dicts,
            success_count=sum(1 for rd in row_dicts if rd["상태"] == step4_excel.STATUS_OK),
            check_count=sum(1 for rd in row_dicts if rd["상태"].startswith(step4_excel.STATUS_CHECK)),
            fail_count=sum(1 for rd in row_dicts if rd["상태"].startswith(step4_excel.STATUS_FAIL)),
        )
    except step1_search.KaptAccessBlocked as e:
        job.update(status="error", message=str(e))
    except Exception as e:
        job.update(status="error", message=f"수집 중 오류가 발생했습니다({e})")


@app.post("/api/collect")
async def collect(req: CollectRequest):
    # 요청마다 크로미움이 여러 개 떠서, 동시 실행을 허용하면 무료 인스턴스 메모리가 바로 바닥난다.
    if any(j["status"] == "running" for j in JOBS.values()):
        raise HTTPException(
            status_code=429, detail="다른 수집 작업이 진행 중입니다. 잠시 후 다시 시도해주세요."
        )
    _cleanup_old_jobs()

    job_id = uuid.uuid4().hex[:12]
    job_dir = os.path.join(STORAGE_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    JOBS[job_id] = {"status": "running", "message": "작업을 시작하는 중", "done": 0, "total": 0}

    task = asyncio.create_task(_run_job(job_id, req, job_dir))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"job_id": job_id}


@app.get("/api/collect/{job_id}")
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404, detail="작업을 찾을 수 없습니다(서버가 재시작되었을 수 있음)."
        )
    return job


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
