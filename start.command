#!/bin/zsh
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python이 없습니다. https://www.python.org/downloads/ 에서 설치해주세요."
  read -r "?엔터를 누르면 창이 닫힙니다."
  exit 1
fi
# 업데이트가 이 파일을 덮어쓸 수 있어 exec로 넘긴다
exec python3 launcher.py
