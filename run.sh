#!/usr/bin/env bash
# 클래스매니저 실행 스크립트
# 사용법: bash run.sh
#   -> http://localhost:5002 (포트 충돌 시 다음 빈 포트 자동 사용)
set -euo pipefail
cd "$(dirname "$0")"

PORT=5002
if command -v ss >/dev/null 2>&1; then
  while ss -ltn 2>/dev/null | grep -q ":${PORT} "; do
    echo "[run.sh] 포트 ${PORT}이 사용 중이라 다음 포트를 찾습니다."
    PORT=$((PORT+1))
  done
elif command -v lsof >/dev/null 2>&1; then
  while lsof -i :${PORT} >/dev/null 2>&1; do
    echo "[run.sh] 포트 ${PORT}이 사용 중이라 다음 포트를 찾습니다."
    PORT=$((PORT+1))
  done
fi

if [ ! -f "classmanager.db" ]; then
  echo "[run.sh] 데모 데이터를 생성합니다..."
  python3 seed.py
fi

export PORT
echo "[run.sh] 클래스매니저 실행 중: http://localhost:${PORT}"
echo "[run.sh] 종료하려면 Ctrl+C를 누르세요."
python3 app.py
