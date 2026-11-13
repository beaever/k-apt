// 데스크톱 앱: 수집 서버(Python)를 뒤에서 띄우고, 서버가 내주는 화면을 창에 연다.
// 설치본은 resources/ 안의 Python·서버 코드·화면·수집용 브라우저를 쓰고(prepare.py가 채움),
// 개발 실행(npm start)은 저장소의 backend/venv와 frontend/out을 쓴다.
const { app, BrowserWindow, dialog, shell } = require("electron");
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const WINDOWS = process.platform === "win32";
const READY_TIMEOUT_MS = 180_000;
const RESOURCES = app.isPackaged ? process.resourcesPath : path.join(__dirname, "..");
const BACKEND_DIR = path.join(RESOURCES, "backend");
const FRONTEND_DIR = app.isPackaged ? path.join(RESOURCES, "frontend") : path.join(RESOURCES, "frontend", "out");
const PYTHON_HOME = app.isPackaged ? path.join(RESOURCES, "python") : path.join(BACKEND_DIR, "venv");
const PYTHON = WINDOWS
  ? path.join(PYTHON_HOME, app.isPackaged ? "python.exe" : path.join("Scripts", "python.exe"))
  : path.join(PYTHON_HOME, "bin", "python3");

let backend = null;
let backendLog = "";
let quitting = false;

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function startBackend(port) {
  backendLog = path.join(app.getPath("userData"), "backend.log");
  const log = fs.openSync(backendLog, "w");
  const env = {
    ...process.env,
    KAPT_STORAGE_DIR: path.join(app.getPath("userData"), "storage"),
    KAPT_FRONTEND_DIR: FRONTEND_DIR,
    // 한글 Windows의 기본 인코딩(cp949)으로는 로그에 못 쓰는 글자가 있다
    PYTHONUTF8: "1",
    PYTHONIOENCODING: "utf-8",
  };
  if (app.isPackaged) env.PLAYWRIGHT_BROWSERS_PATH = path.join(RESOURCES, "browsers");
  backend = spawn(PYTHON, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(port)], {
    cwd: BACKEND_DIR,
    env,
    stdio: ["ignore", log, log],
    windowsHide: true,
    // macOS: 자식(크로미움, 분석 프로세스)까지 한 번에 끌 수 있게 별도 그룹으로 띄운다
    detached: !WINDOWS,
  });
  backend.once("exit", (code) => {
    backend = null;
    if (!quitting) fail(`수집 서버가 예기치 않게 꺼졌습니다(코드 ${code}).`);
  });
  backend.once("error", (e) => {
    backend = null;
    if (!quitting) fail(`수집 서버를 켜지 못했습니다(${e.message}).`);
  });
}

function stopBackend() {
  if (!backend) return;
  const { pid } = backend;
  backend = null;
  if (WINDOWS) {
    // kill()은 자식(크로미움, 분석 프로세스)을 남기므로 프로세스 트리 전체를 끈다
    spawnSync("taskkill", ["/T", "/F", "/PID", String(pid)]);
    return;
  }
  try {
    process.kill(-pid, "SIGTERM");
  } catch {
    // 이미 꺼짐
  }
}

async function waitUntilReady(port) {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!backend) return false;
    try {
      if ((await fetch(`http://127.0.0.1:${port}/`)).ok) return true;
    } catch {
      // 아직 켜지는 중
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

function logTail() {
  try {
    return fs.readFileSync(backendLog, "utf8").trim().split("\n").slice(-15).join("\n");
  } catch {
    return "";
  }
}

function fail(message) {
  if (quitting) return;
  quitting = true;
  dialog.showErrorBox(
    "K-apt 낙찰 정보 수집기",
    `${message}\n\n이 창을 캡처해 담당자에게 전달해주세요.\n기록 파일: ${backendLog}\n\n${logTail()}`,
  );
  app.quit();
}

function checkForUpdates() {
  if (!app.isPackaged) return;
  // 새 버전은 뒤에서 받아 두고 앱을 끌 때 설치한다. 확인에 실패해도 지금 버전으로 계속 쓴다.
  const { autoUpdater } = require("electron-updater");
  autoUpdater.on("error", (e) => console.error("업데이트 확인 실패:", e.message));
  autoUpdater.checkForUpdatesAndNotify().catch(() => {});
}

async function main() {
  const win = new BrowserWindow({ width: 1440, height: 900, minWidth: 900, minHeight: 600, autoHideMenuBar: true });
  const loading = "<html style='color-scheme:light dark'><body style='font-family:sans-serif;display:grid;place-items:center;height:100vh;margin:0;color:GrayText'>프로그램을 켜는 중이에요…</body>";
  win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(loading)}`);
  // 공고 원문·K-apt 링크는 앱 창이 아니라 사용자의 기본 브라우저에서 연다
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });

  // 점검용으로 포트를 고정할 수 있다 (desktop-build.yml)
  const port = Number(process.env.KAPT_PORT) || (await freePort());
  startBackend(port);
  if (await waitUntilReady(port)) {
    await win.loadURL(`http://127.0.0.1:${port}/`);
    checkForUpdates();
  } else if (backend) {
    fail("수집 서버가 제시간에 켜지지 않았습니다.");
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const [win] = BrowserWindow.getAllWindows();
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", () => {
    quitting = true;
    stopBackend();
  });
  app.whenReady().then(main);
}
