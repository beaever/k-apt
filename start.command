#!/bin/zsh
cd "$(dirname "$0")" || exit 1
PY=runtime/python/bin/python3

fail() {
  echo "\n[오류] Python을 내려받지 못했습니다. 인터넷 연결을 확인한 뒤 다시 실행해주세요."
  read -r "?이 창을 캡처해 담당자에게 전달해주세요. 엔터를 누르면 창이 닫힙니다."
  exit 1
}

if [[ ! -x $PY ]]; then
  echo "처음 실행 준비 중입니다. Python을 내려받습니다..."
  if [[ $(uname -m) == arm64 ]]; then
    BUILD=aarch64-apple-darwin
    HASH=30a18bcb9ab75e485b4bf4e5d783dde165571e7e42c56dff47a2d2c5345183a4
  else
    BUILD=x86_64-apple-darwin
    HASH=01cedae5d4a66ac570e13cb35fa570d0e79ba8e9affd4518a5d85b6c2b98ff10
  fi
  rm -rf runtime/tmp
  mkdir -p runtime/tmp || fail
  curl -fL -o runtime/tmp/python.tar.gz "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.14.10%2B20261026-$BUILD-install_only.tar.gz" || fail
  echo "$HASH  runtime/tmp/python.tar.gz" | shasum -a 256 -c -s || fail
  # 압축 해제가 중간에 끊겨도 다음 실행 때 다시 받도록 다 푼 뒤에 옮긴다
  tar -xf runtime/tmp/python.tar.gz -C runtime/tmp || fail
  mv runtime/tmp/python runtime/python || fail
  rm -rf runtime/tmp
fi

# 업데이트가 이 파일을 덮어쓸 수 있어 exec로 넘긴다
exec "$PY" launcher.py
