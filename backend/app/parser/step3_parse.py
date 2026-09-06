"""3단계: 첨부 공고문(PDF/HWP) 텍스트 파싱 -> 자본금/실적/공법번호 추출.

값을 못 찾았을 때는 빈 문자열이 아니라 "왜 없는지"를 그 자리에 채운다
(첨부파일 자체가 없는지 / 다운로드가 실패했는지 / 텍스트 추출이 안 됐는지 / 문서에 해당 문구가 없는지).
"""

import os
import re
import subprocess

import pdfplumber

FIELDS = ["capital", "record", "patent_no"]

PATENT_NO = re.compile(r"특허\s*제?\s*([\d\-]+)\s*호")
# "실적" 단어 없이 "최근 N년간 ... M건 이상"으로만 표현되는 공고문 대비 폴백 패턴
RECORD_FALLBACK = re.compile(r"최근\s*\d+\s*년간.*?\d+\s*건\s*이상")

DOWNLOAD_FAILED_REASON = "공고문을 확보하지 못해 확인 불가"
NOT_FOUND_REASON = "공고문 내용에서 해당 항목 관련 문구를 찾지 못함"


def extract_text(path: str) -> tuple[str, str | None]:
    """(추출된 텍스트, 실패 사유) 반환. 성공하면 실패 사유는 None."""
    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        try:
            with pdfplumber.open(path) as pdf:
                text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception as e:
            return "", f"PDF를 열지 못함({e})"
        if not text.strip():
            return "", "PDF에서 텍스트를 추출하지 못함(스캔 이미지 PDF일 가능성)"
        return text, None

    if ext == ".hwp":
        result = subprocess.run(["hwp5txt", path], capture_output=True, text=True)
        if result.returncode != 0:
            reason = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "알 수 없는 오류"
            return "", f"HWP 파싱 실패({reason[:100]})"
        if not result.stdout.strip():
            return "", "HWP에서 텍스트를 추출하지 못함"
        return result.stdout, None

    # ponytail: .hwpx(OOXML)/.zip 등은 의뢰서 범위(HWP/PDF) 밖 -> 미지원, 필요해지면 zipfile+xml로 추가
    return "", f"지원하지 않는 첨부파일 형식({ext or '확장자 없음'})이라 텍스트 추출 불가"


def parse_requirements(text: str) -> dict:
    """공고문 텍스트에서 자본금/실적/공법번호 요건이 적힌 줄을 그대로 추출.

    수치를 엄격히 정규화하지 않고 요건 문장 전체를 값으로 사용 -> 표현이 제각각인
    공고문에서도 안정적으로 사람이 바로 읽을 수 있는 값을 얻는다.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    capital = next((line for line in lines if "자본금" in line), "")
    record = next((line for line in lines if "실적" in line), "")
    if not record:
        record = next((line for line in lines if RECORD_FALLBACK.search(line)), "")
    patent_line = next((line for line in lines if "특허" in line or "공법" in line), "")
    m = PATENT_NO.search(patent_line)
    patent_no = m.group(1) if m else patent_line
    return {"capital": capital, "record": record, "patent_no": patent_no}


def parse_files(paths: list[str], fetch_error: str | None = None) -> dict:
    if not paths:
        reason = fetch_error or DOWNLOAD_FAILED_REASON
        return dict.fromkeys(FIELDS, reason)

    combined = dict.fromkeys(FIELDS, "")
    extract_errors: list[str] = []
    any_text_extracted = False

    for path in paths:
        text, err = extract_text(path)
        if err:
            extract_errors.append(f"{os.path.basename(path)}: {err}")
            continue
        any_text_extracted = True
        for key, value in parse_requirements(text).items():
            if value and not combined[key]:
                combined[key] = value

    if not any_text_extracted:
        reason = "; ".join(extract_errors) if extract_errors else "첨부파일에서 텍스트를 추출하지 못함"
        return dict.fromkeys(FIELDS, reason)

    for key in FIELDS:
        if not combined[key]:
            combined[key] = NOT_FOUND_REASON
    return combined
