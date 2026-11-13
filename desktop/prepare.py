"""설치 파일에 넣을 부품을 desktop/resources/에 채운다: Python + 수집 서버 부품, 수집용 브라우저, 글자 인식 모델, 화면.

지금 실행 중인 OS용으로 만든다 (Windows 설치 파일은 Windows에서). 표준 라이브러리만 쓴다.
사용법: python desktop/prepare.py  ->  cd desktop && npm ci && npm run dist
"""

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend")
RESOURCES = os.path.join(ROOT, "desktop", "resources")
PYTHON_DIR = os.path.join(RESOURCES, "python")
WINDOWS = os.name == "nt"
PYTHON = os.path.join(PYTHON_DIR, "python.exe") if WINDOWS else os.path.join(PYTHON_DIR, "bin", "python3")

# start.bat / start.command와 같은 Python (python-build-standalone). 플랫폼 -> (빌드 이름, SHA-256)
PYTHON_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.14.10%2B20261026-{build}-install_only.tar.gz"
PYTHON_BUILDS = {
    ("Windows", "AMD64"): ("x86_64-pc-windows-msvc", "98bbe6a0d798bcbad142e96e5930a330e4dc407638e898b515c51eb73a27580d"),
    ("Darwin", "arm64"): ("aarch64-apple-darwin", "30a18bcb9ab75e485b4bf4e5d783dde165571e7e42c56dff47a2d2c5345183a4"),
    ("Darwin", "x86_64"): ("x86_64-apple-darwin", "01cedae5d4a66ac570e13cb35fa570d0e79ba8e9affd4518a5d85b6c2b98ff10"),
}


def run(*cmd, cwd=None, env=None):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})}, check=True)


def fetch_python():
    if os.path.exists(PYTHON):
        return
    build, sha256 = PYTHON_BUILDS[(platform.system(), platform.machine())]
    archive = os.path.join(RESOURCES, "python.tar.gz")
    print("Python 내려받는 중...", flush=True)
    urllib.request.urlretrieve(PYTHON_URL.format(build=build), archive)
    with open(archive, "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() != sha256:
            raise SystemExit("Python 압축 파일의 해시가 맞지 않습니다.")
    with tarfile.open(archive) as tar:
        tar.extractall(RESOURCES, filter="data")
    os.remove(archive)


def main():
    os.makedirs(RESOURCES, exist_ok=True)
    fetch_python()
    run(PYTHON, "-m", "pip", "install", "-q", "--no-warn-script-location", "-r", os.path.join(BACKEND, "requirements.txt"))
    # 수집은 화면 없는 크로미움만 쓴다 (--only-shell: 전체 크로미움보다 약 350MB 작음)
    run(
        PYTHON, "-m", "playwright", "install", "--only-shell", "chromium",
        env={"PLAYWRIGHT_BROWSERS_PATH": os.path.join(RESOURCES, "browsers")},
    )
    # 한국어 글자 인식 모델은 처음 쓸 때 내려받는다 -> 설치본은 설치 폴더에 쓸 수 없을 수 있어 미리 받아 둔다
    run(PYTHON, "-c", "from app.parser.step3_parse import _ocr_engine; _ocr_engine()", cwd=BACKEND)
    for folder, dirs, _ in os.walk(PYTHON_DIR):
        if "__pycache__" in dirs:
            shutil.rmtree(os.path.join(folder, "__pycache__"))
            dirs.remove("__pycache__")

    npm = "npm.cmd" if WINDOWS else "npm"
    if not os.path.isdir(os.path.join(FRONTEND, "node_modules")):
        run(npm, "ci", "--no-audit", "--no-fund", cwd=FRONTEND)
    # API 주소를 비워 화면이 자신을 내준 서버(같은 주소)로 요청하게 한다
    run(npm, "run", "build", cwd=FRONTEND, env={"DESKTOP_BUILD": "1", "NEXT_PUBLIC_API_BASE": ""})
    print("준비 완료:", RESOURCES)


if __name__ == "__main__":
    sys.exit(main())
