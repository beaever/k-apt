"""실제 수집과 같은 경로(별도 분석 프로세스, spawn)로 공고문 1건을 분석해 본다. K-apt에는 접속하지 않는다.

Windows에서 분석 프로세스가 뜨지 못하면 모든 공고가 "수집 실패: 공고문 분석 중 오류"가 된다 -> CI에서 미리 잡는다.
사용법: backend 폴더에서 python tests/check_parse_pool.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import main  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "requirement_in_table.hwp")


async def check():
    results, job = [None], {"done": 0}
    await main._parse_and_store(0, {"bid_num": "check"}, {"files": [FIXTURE], "download_error": None}, results, job)
    r = results[0]
    assert r["success"], f"분석 프로세스 오류: {r.get('error')}"
    assert r.get("capital_found"), r
    print("분석 프로세스 정상:", r["capital"])


# spawn 방식의 분석 프로세스는 이 파일을 다시 불러오므로, 실행 코드는 반드시 이 안에 둔다
if __name__ == "__main__":
    asyncio.run(check())
