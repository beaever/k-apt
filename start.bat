@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 업데이트가 실행 중인 이 파일을 덮어쓸 수 있어, 실행 후 파일의 다음 줄을 읽지 않도록 한 줄에서 끝낸다
py -3 --version >nul 2>&1 && (py -3 launcher.py & exit /b)
python --version >nul 2>&1 && (python launcher.py & exit /b)
echo Python이 없습니다. https://www.python.org/downloads/ 에서 설치해주세요.
echo 설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
pause
