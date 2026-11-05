@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=runtime\python\python.exe"
if exist "%PY%" goto run

echo 처음 실행 준비 중입니다. Python을 내려받습니다...
if not exist runtime mkdir runtime
rmdir /s /q runtime\tmp 2>nul
mkdir runtime\tmp
curl -fL -o runtime\tmp\python.tar.gz "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.14.10%%2B20261026-x86_64-pc-windows-msvc-install_only.tar.gz" || goto fail
certutil -hashfile runtime\tmp\python.tar.gz SHA256 | findstr /i "98bbe6a0d798bcbad142e96e5930a330e4dc407638e898b515c51eb73a27580d" >nul || goto fail
rem 압축 해제가 중간에 끊겨도 다음 실행 때 다시 받도록 다 푼 뒤에 옮긴다
tar -xf runtime\tmp\python.tar.gz -C runtime\tmp || goto fail
move runtime\tmp\python runtime\python >nul || goto fail
rmdir /s /q runtime\tmp

:run
rem 업데이트가 실행 중인 이 파일을 덮어쓸 수 있어, 실행 후 파일의 다음 줄을 읽지 않도록 한 줄에서 끝낸다
"%PY%" launcher.py & exit /b

:fail
echo.
echo [오류] Python을 내려받지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행해주세요.
echo 계속 안 되면 이 창을 캡처해 담당자에게 전달해주세요.
pause
