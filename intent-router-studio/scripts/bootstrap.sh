#!/usr/bin/env bash
# 安装后端与前端依赖并初始化数据库
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 后端虚拟环境"
if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
fi
backend/.venv/bin/pip install --upgrade pip >/dev/null
backend/.venv/bin/pip install -e "backend[dev]"

echo "==> 初始化数据库"
(cd backend && .venv/bin/python -c "from app.db import init_db; init_db(); print('db ok')")

echo "==> 前端依赖"
(cd frontend && npm install)

echo "==> 生成示例数据"
python3 scripts/make_example_data.py

echo "bootstrap 完成。使用 make dev 启动开发环境。"
