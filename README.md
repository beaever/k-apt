# K-apt 아스콘 입찰 정보 수집기

[K-apt(공동주택관리정보시스템)](https://www.k-apt.go.kr)에서 아스콘(아스팔트 콘크리트) 공사 낙찰 공고를 검색하고,
공고문(첨부파일 또는 외부 원문)에서 자본금·실적·공법번호 같은 비정형 요건을 자동으로 추출해
엑셀 파일로 내려받는 웹 서비스입니다.

## 주요 기능

- 지역(서울/인천/경기), 검색어, 검색 기간(직접 지정, 최대 1년)으로 낙찰 공고 검색
- 검색 결과 페이지네이션을 전부 순회해 조건에 맞는 **모든** 공고 수집 (건수 제한 없음)
- 공고 상세페이지 접속 후 아파트명/세대수/낙찰업체/낙찰금액 등 자동 추출
- 첨부된 입찰공고문(HWP/PDF) 자동 다운로드 후 텍스트 추출
- K-apt 자체 첨부파일이 없는 공고는 "해당 공고 가기"를 통해 외부 대행 사이트(a2p.kr, kg2b.com 등)의
  원문 공고서까지 따라가서 확보
- 공고문 텍스트에서 자본금 / 실적 / 공법(특허)번호 요건을 정규식으로 추출
- 값을 찾지 못한 경우 빈 칸이 아니라 **왜 못 찾았는지 구체적인 사유**를 표시
  (첨부파일 없음 / 다운로드 실패 사유 / 지원하지 않는 파일 형식 / 문서에 해당 문구 없음 등)
- 5개 공고를 동시에 처리하는 병렬 크롤링으로 속도 개선 (워커별 독립 브라우저 세션 사용)
- 결과를 화면 표로 즉시 확인 (성공/실패 건수, 실패 행 빨간색 강조) + 엑셀 다운로드

## 기술 스택

| 영역 | 기술 |
|---|---|
| 백엔드 | Python, FastAPI, Playwright(Chromium), pdfplumber, pyhwp, BeautifulSoup4, openpyxl |
| 프런트엔드 | Next.js(App Router), React, TypeScript, Tailwind CSS |

인증/결제 기능은 포함하지 않습니다.

## 프로젝트 구조

```
kapt-asphalt-collector/
  backend/
    app/
      main.py              # FastAPI 앱, /api/collect 엔드포인트
      crawler/
        step1_search.py    # K-apt 접속 → 지역/검색어/기간 필터 → 목록 수집(페이지네이션 전체)
        step2_download.py  # 상세페이지 접속 → 첨부파일(또는 외부 원문) 다운로드
      parser/
        step3_parse.py     # PDF/HWP 텍스트 추출 + 자본금/실적/공법번호 정규식 파싱
      export/
        step4_excel.py     # 결과 병합 후 엑셀(.xlsx) 생성, 상태별 색상 표시
      storage/              # 요청(job)별 다운로드 파일 및 생성된 엑셀 (git 미포함)
    requirements.txt
    tests/
      test_parsing.py      # 파싱 로직 자체 점검 스크립트
  frontend/
    app/page.tsx            # 검색 폼 + 진행 상태 + 결과 표 + 엑셀 다운로드 버튼
```

## 실행 방법

### 1. 백엔드 (FastAPI)

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn app.main:app --host 127.0.0.1 --port 8420
```

HWP 텍스트 추출은 `pyhwp`가 설치하는 `hwp5txt` CLI를 사용합니다(위 `pip install`로 함께 설치됩니다).

### 2. 프런트엔드 (Next.js)

```bash
cd frontend
npm install
npm run dev
```

`frontend/.env.local`에 백엔드 주소를 지정합니다 (기본값):

```
NEXT_PUBLIC_API_BASE=http://127.0.0.1:8420
```

개발 서버 접속 시 `http://localhost:3000`으로 접속하세요. (`127.0.0.1`로 접속하면 Next.js
개발 서버의 HMR 웹소켓이 차단되어 화면이 계속 새로고침되는 문제가 있습니다.)

## 사용 방법

1. 검색할 지역(서울/인천/경기)을 체크
2. 검색어 입력 (예: "아스콘")
3. 검색 기간을 직접 지정하거나 빠른 선택(1주일/1개월/1년) 버튼 사용 (최대 1년)
4. "검색 시작" 클릭 → 진행 상황이 표시되며 완료 시 결과 표와 성공/실패 건수 표시
5. "엑셀 다운로드" 버튼으로 결과를 `.xlsx` 파일로 저장
   (파일 저장 위치는 브라우저의 기본 다운로드 설정을 따릅니다)

엑셀/결과 표의 컬럼: 공고일자 / 아파트명 / 공사명 / 세대수 / 자본금 / 실적 / 공법번호 /
낙찰방법 / 낙찰업체 / 낙찰금액 / 상태

## 동작 원리 (4단계)

1. **검색(step1_search.py)** — Playwright로 K-apt 메인 페이지 진입 후 메뉴 클릭으로
   "사업자 선정(경쟁입찰) 결과 공개" 페이지 접속 (직접 URL 접근은 세션 미인증 에러 발생).
   지역/검색어/기간 필터 적용 후 전체 페이지를 순회하며 공고 목록 수집.
2. **상세/다운로드(step2_download.py)** — 공고별 상세페이지에서 정보 추출 및 첨부파일 다운로드.
   K-apt 자체 첨부파일이 없으면 "해당 공고 가기" 버튼으로 외부 대행 사이트에 진입해 원문을 확보.
3. **파싱(step3_parse.py)** — 다운로드한 PDF/HWP에서 텍스트를 추출하고, 자본금/실적/공법(특허)번호가
   적힌 줄을 정규식으로 찾아냄. 못 찾으면 사유를 값 대신 기록.
4. **엑셀 출력(step4_excel.py)** — 위 결과를 병합해 `.xlsx`로 저장, 실패 건은 빨간색으로 강조.

## 테스트

파싱 로직에 대한 최소한의 자체 점검 스크립트가 있습니다.

```bash
cd backend
source venv/bin/activate
python tests/test_parsing.py
```

## 알려진 제한사항

- 상세페이지/첨부파일 구조가 K-apt 자체 전자입찰, a2p.kr, kg2b.com 등 대행사별로 달라
  확인된 레이아웃 외의 새로운 형태가 나오면 파싱이 실패할 수 있습니다(이 경우 실패 사유가 표시됩니다).
- `.hwpx` 등 HWP/PDF 이외의 첨부파일 형식은 텍스트 추출을 지원하지 않습니다.
- 요건 값은 정규식/키워드 기반 추출이므로 문서 표현이 특이한 경우 놓칠 수 있습니다.
- K-apt 사이트 구조가 변경되면 selector를 다시 확인해야 합니다.
