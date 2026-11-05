"""더블클릭 실행기: 최신 main 적용 -> Node.js·부품 설치 -> 서버 2개 실행 -> 브라우저 열기.

start.bat(Windows) / start.command(macOS)가 프로젝트 폴더에 Python을 받아 호출한다.
venv를 만들기 전에 돌아야 해서 표준 라이브러리만 쓴다.
구버전 Python에서도 버전 안내까지는 도달해야 하므로 3.10 이상 전용 문법(X | Y 등)을 쓰지 않는다.
"""

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
import zipfile

REPO = "beaever/k-apt"
NODE_VERSION = "v22.23.4"
# 플랫폼 -> (압축 확장자, SHA-256). nodejs.org의 SHASUMS256.txt 값.
NODE_BUILDS = {
    "win-x64": ("zip", "cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20"),
    "darwin-arm64": ("tar.gz", "c9e1dbc748b33ed2d14f42445191ecaf14c56259c7a9a26d0042c49ec2b66428"),
    "darwin-x64": ("tar.gz", "efcc2b0db6f94bf53eddb038fe2b370277a042bd29e4f19b8f8e23598795b819"),
}
# step4_excel.py의 f-string 문법이 3.12부터 동작한다
MIN_PYTHON = (3, 12)
BACKEND_PORT = 8420
FRONTEND_PORT = 3000
APP_URL = f"http://localhost:{FRONTEND_PORT}"  # 127.0.0.1로 열면 화면이 계속 새로고침될 수 있음
READY_TIMEOUT_SEC = 180

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend")
VENV = os.path.join(BACKEND, "venv")
VENV_PYTHON = os.path.join(VENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(VENV, "bin", "python")
RUNTIME = os.path.join(ROOT, "runtime")
NODE_DIR = os.path.join(RUNTIME, "node")
STATE_DIR = os.path.join(ROOT, ".launcher")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
# 잘못된 압축 파일로 설치본을 덮어쓰지 않도록 적용 전에 확인한다
REQUIRED_FILES = ("launcher.py", "backend/app/main.py", "backend/requirements.txt", "frontend/package-lock.json")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


class LauncherError(Exception):
    """사용자에게 그대로 보여줄 안내 문구."""


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def curl(*args, timeout=120):
    # urllib 대신 curl: python.org의 macOS용 Python은 인증서를 따로 설치하지 않으면 HTTPS가 실패한다
    return subprocess.run(
        ["curl", "-fsSL", "--connect-timeout", "5", *args],
        check=True, capture_output=True, timeout=timeout,
    ).stdout


def latest_commit():
    body = curl("-H", "Accept: application/vnd.github.sha", f"https://api.github.com/repos/{REPO}/commits/main", timeout=15)
    commit = body.decode("utf-8", "replace").strip()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ValueError(f"잘못된 버전 값: {commit[:80]!r}")
    return commit


def apply_update(zip_path, root, commit, state):
    """압축 파일을 검증한 뒤 설치 폴더에 덮어쓴다. venv/node_modules/결과 파일은 압축에 없어 유지된다.

    버전 기록은 맨 마지막에 하므로 도중에 실패하면 다음 실행 때 다시 적용된다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        entries = os.listdir(tmp)
        source = os.path.join(tmp, entries[0]) if len(entries) == 1 else tmp
        if not all(os.path.isfile(os.path.join(source, f)) for f in REQUIRED_FILES):
            raise ValueError("압축 파일에 프로그램 파일이 없음")

        files = []
        for folder, _, names in os.walk(source):
            for name in names:
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, source).replace(os.sep, "/")
                files.append(rel)
                target = os.path.join(root, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(path, target)

    # 이전 버전에 있다가 사라진 파일만 지운다 (기록에 없는 파일은 사용자 것일 수 있어 건드리지 않음)
    for rel in set(state.get("files", [])) - set(files):
        try:
            os.remove(os.path.join(root, rel))
        except FileNotFoundError:
            pass
    # zipfile은 실행 권한을 복원하지 않는다
    if os.name != "nt":
        os.chmod(os.path.join(root, "start.command"), 0o755)

    state.update(version=commit, files=files)


def update(state):
    """main에 새 커밋이 있으면 적용하고 True. 실패해도 지금 버전으로 계속 쓸 수 있어야 하므로 예외를 올리지 않는다."""
    # 개발 폴더(git 체크아웃)의 작업 중인 코드를 덮어쓰지 않는다
    if os.path.exists(os.path.join(ROOT, ".git")):
        return False
    print("새 버전 확인 중...")
    try:
        commit = latest_commit()
        if commit == state.get("version"):
            return False
        print(f"새 버전({commit[:7]})을 적용합니다...")
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "release.zip")
            curl(f"https://github.com/{REPO}/archive/{commit}.zip", "-o", zip_path)
            apply_update(zip_path, ROOT, commit, state)
        save_state(state)
        return True
    except (subprocess.SubprocessError, OSError, ValueError, zipfile.BadZipFile) as e:
        print(f"업데이트를 적용하지 못해 지금 버전으로 실행합니다. 다음 실행 때 다시 시도합니다. ({e})")
        return False


def file_hash(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def ensure_node():
    """프로젝트 폴더의 Node.js를 쓰도록 PATH를 잡고 npm 경로를 반환한다. 없으면 내려받는다."""
    bin_dir = NODE_DIR if os.name == "nt" else os.path.join(NODE_DIR, "bin")
    npm = os.path.join(bin_dir, "npm.cmd" if os.name == "nt" else "npm")
    if not os.path.exists(npm):
        print("화면 실행에 필요한 Node.js를 내려받습니다...")
        if os.name == "nt":
            build = "win-x64"
        else:
            build = "darwin-arm64" if platform.machine() == "arm64" else "darwin-x64"
        extension, expected_hash = NODE_BUILDS[build]
        folder = f"node-{NODE_VERSION}-{build}"
        os.makedirs(RUNTIME, exist_ok=True)
        # 같은 디스크에서 옮기도록 runtime 안에 푼다. 끊겨도 node 폴더가 불완전하게 남지 않는다.
        with tempfile.TemporaryDirectory(dir=RUNTIME) as tmp:
            archive = os.path.join(tmp, f"{folder}.{extension}")
            try:
                curl(f"https://nodejs.org/dist/{NODE_VERSION}/{folder}.{extension}", "-o", archive, timeout=900)
                if file_hash(archive) != expected_hash:
                    raise LauncherError("내려받은 Node.js 파일이 손상되었습니다. 다시 실행해주세요.")
                subprocess.run(["tar", "-xf", archive, "-C", tmp], check=True, capture_output=True)
            except subprocess.SubprocessError as e:
                raise LauncherError(f"Node.js를 내려받지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행해주세요. ({e})")
            os.replace(os.path.join(tmp, folder), NODE_DIR)
    os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    return npm


def install(state, npm):
    """부품 목록이 바뀌었을 때만 설치한다. 기록은 설치 성공 후에 하므로 실패하면 다음 실행 때 다시 시도된다."""
    # venv는 만든 위치의 절대 경로에 묶여 있어 폴더를 옮기면 다시 만들어야 한다
    if state.get("root") not in (None, ROOT) and os.path.isdir(VENV):
        print("폴더 위치가 바뀌어 수집 서버 부품을 다시 설치합니다...")
        shutil.rmtree(VENV)
    state["root"] = ROOT
    save_state(state)

    requirements = os.path.join(BACKEND, "requirements.txt")
    backend_hash = file_hash(requirements)
    if state.get("backend") != backend_hash or not os.path.exists(VENV_PYTHON):
        print("수집 서버 부품을 설치합니다. 처음에는 5~10분 걸릴 수 있어요...")
        if not os.path.exists(VENV_PYTHON):
            subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
        subprocess.run([VENV_PYTHON, "-m", "pip", "install", "-q", "-r", requirements], check=True)
        subprocess.run([VENV_PYTHON, "-m", "playwright", "install", "chromium"], check=True)
        state["backend"] = backend_hash
        save_state(state)

    frontend_hash = file_hash(os.path.join(FRONTEND, "package-lock.json"))
    if state.get("frontend") != frontend_hash or not os.path.isdir(os.path.join(FRONTEND, "node_modules")):
        print("화면 부품을 설치합니다...")
        subprocess.run([npm, "ci", "--no-audit", "--no-fund", "--no-progress", "--loglevel=error"], cwd=FRONTEND, check=True)
        state["frontend"] = frontend_hash
        save_state(state)


def port_open(port):
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except OSError:
        return False


def start_process(cmd, cwd, log_path):
    with open(log_path, "wb") as log:
        # macOS: 자식 프로세스까지 한 번에 끌 수 있도록 별도 그룹으로 띄운다 (창을 닫으면 _raise_interrupt가 받음)
        return subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != "nt")


def stop_process(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        # terminate()는 자식(크로미움, node)을 남기므로 프로세스 트리 전체를 끈다
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)


def log_tail(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return "".join(f.readlines()[-15:])


def serve(npm):
    os.makedirs(STATE_DIR, exist_ok=True)
    backend_log = os.path.join(STATE_DIR, "backend.log")
    frontend_log = os.path.join(STATE_DIR, "frontend.log")
    procs = {}
    try:
        print("프로그램을 켜는 중...")
        procs[backend_log] = start_process(
            [VENV_PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
            BACKEND, backend_log,
        )
        # next dev는 기본으로 같은 네트워크의 다른 기기에도 열리므로 이 PC로 제한한다
        procs[frontend_log] = start_process([npm, "run", "dev", "--", "--hostname", "localhost"], FRONTEND, frontend_log)

        deadline = time.time() + READY_TIMEOUT_SEC
        opened = False
        while True:
            for log_path, proc in procs.items():
                if proc.poll() is not None:
                    raise LauncherError(f"프로그램이 예기치 않게 꺼졌습니다.\n{log_tail(log_path)}")
            if not opened and port_open(BACKEND_PORT) and port_open(FRONTEND_PORT):
                webbrowser.open(APP_URL)
                print(f"\n준비 완료! 브라우저가 열리지 않으면 {APP_URL} 로 접속하세요.")
                print("다 쓰면 이 창을 닫으면 프로그램도 꺼집니다.\n")
                opened = True
            if not opened and time.time() > deadline:
                raise LauncherError(f"{READY_TIMEOUT_SEC}초가 지나도 프로그램이 켜지지 않았습니다. 기록: {STATE_DIR}")
            time.sleep(1)
    finally:
        for proc in procs.values():
            stop_process(proc)


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def run_updated():
    # subprocess.call은 중단 신호를 받으면 자식을 바로 강제 종료해 서버가 남는다 -> 자식이 정리를 마칠 때까지 기다린다
    proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--no-update"])
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            # Windows는 Ctrl+C와 창 닫기가 콘솔의 모든 프로세스에 직접 전달된다
            if os.name != "nt":
                proc.send_signal(signal.SIGTERM)


def run():
    if sys.version_info < MIN_PYTHON:
        raise LauncherError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 이상이 필요합니다 (지금: {sys.version.split()[0]}).\n"
            "https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행해주세요."
        )
    backend_on, frontend_on = port_open(BACKEND_PORT), port_open(FRONTEND_PORT)
    if backend_on and frontend_on:
        # 이미 켜져 있으면 새로 띄우거나 실행 중인 파일을 덮어쓰지 않는다
        print("이미 켜져 있어 브라우저만 엽니다.")
        webbrowser.open(APP_URL)
        return 0
    if backend_on or frontend_on:
        port = BACKEND_PORT if backend_on else FRONTEND_PORT
        raise LauncherError(f"다른 프로그램이 {port}번 포트를 쓰고 있습니다. 해당 프로그램을 끄거나 PC를 재시작해주세요.")

    state = load_state()
    if "--no-update" not in sys.argv and update(state):
        # 실행기 자신도 바뀌었을 수 있으므로 새 파일로 다시 시작한다
        return run_updated()
    npm = ensure_node()
    install(state, npm)
    serve(npm)
    return 0


def main():
    if os.name != "nt":
        signal.signal(signal.SIGHUP, _raise_interrupt)
        signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        return run()
    except KeyboardInterrupt:
        return 0
    except (LauncherError, subprocess.CalledProcessError, OSError) as e:
        print(f"\n[오류] {e}")
        # 더블클릭으로 연 창은 바로 닫히므로 내용을 읽을 시간을 준다
        input("\n이 내용을 캡처해 담당자에게 전달해주세요. 엔터를 누르면 창이 닫힙니다.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
