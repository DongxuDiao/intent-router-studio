#!/usr/bin/env bash
# 同时启动 API、Worker 与 Vite 开发服务器
set -euo pipefail
cd "$(dirname "$0")/.."

PIDS=()
cleanup() {
  echo "停止开发进程..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

(cd backend && .venv/bin/uvicorn app.main:app --host "${APP_HOST:-127.0.0.1}" --port "${APP_PORT:-8000}") &
PIDS+=($!)

(cd backend && .venv/bin/python -m app.worker.main) &
PIDS+=($!)

(cd frontend && npm run dev) &
PIDS+=($!)

echo "dev 环境已启动: API http://127.0.0.1:8000  Web http://127.0.0.1:5173"
wait
