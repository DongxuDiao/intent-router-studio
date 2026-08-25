#!/usr/bin/env bash
# 质量门禁（修改方案 V2 §5.3）：CI 必须全绿才允许发布。
#
# 用法：
#   ./scripts/ci.sh                # 全部门禁（E2E 需本地栈在 127.0.0.1:8000 运行）
#   SKIP_E2E=1 ./scripts/ci.sh     # 跳过 E2E（无栈环境）
#
# 产出审计制品到 var/ci/：
#   pip-audit-runtime.json   Python 运行时依赖审计（豁免清单见 SECURITY_EXCEPTIONS.md）
#   npm-audit-runtime.json   前端生产依赖审计
#   sbom-python.txt          镜像内 Python 依赖清单（锁定版本的 SBOM 基础）
#   sbom-npm.json            前端依赖树
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
CI_OUT="$ROOT/var/ci"
mkdir -p "$CI_OUT"

TEST_IMAGE="${TEST_IMAGE:-intent-router-studio:test}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- 后端
step "1/7 ruff 静态检查（backend）"
docker run --rm --user root -v "$BACKEND:/work" -w /work --entrypoint sh "$TEST_IMAGE" \
  -c "pip install -q ruff 2>/dev/null; python -m ruff check ."

step "2/7 后端测试（干净容器，无宿主挂载）"
docker run --rm --entrypoint sh "$TEST_IMAGE" -c "python -m pytest tests/ -q"

step "3/7 alembic 迁移漂移检查"
docker run --rm --entrypoint sh "$TEST_IMAGE" \
  -c "export DATABASE_URL=sqlite:////tmp/check.db ARTIFACT_ROOT=/tmp/var && \
      python -c 'from app.db_migrate import run_migrations; run_migrations()' >/dev/null && \
      alembic check"

step "4/7 pip-audit 运行时依赖（豁免见 SECURITY_EXCEPTIONS.md）"
# 豁免清单与 SECURITY_EXCEPTIONS.md 保持同步：仅放行有失效日期的已知项
EXEMPT_IDS=(
  PYSEC-2025-217 PYSEC-2026-2290 PYSEC-2026-2288 PYSEC-2026-2289
  PYSEC-2026-139 PYSEC-2025-194 PYSEC-2026-2286 CVE-2025-3001
)
IGNORE_ARGS=()
for id in "${EXEMPT_IDS[@]}"; do IGNORE_ARGS+=(--ignore-vuln "$id"); done
docker run --rm --user root -v "$BACKEND:/work" -v "$CI_OUT:/ci" -w /work --entrypoint sh "$TEST_IMAGE" \
  -c "pip install -q pip-audit 2>/dev/null; python -m pip_audit -r requirements.lock -f json -o /ci/pip-audit-runtime.json ${IGNORE_ARGS[*]}" \
  || { echo "pip-audit 发现未豁免漏洞，详见 $CI_OUT/pip-audit-runtime.json"; exit 1; }
echo "pip-audit 通过（豁免 $((${#EXEMPT_IDS[@]})) 项，见 SECURITY_EXCEPTIONS.md）"

# ---------------------------------------------------------------- 前端
step "5/7 前端单测 + 类型检查 + 构建"
(cd "$FRONTEND" && npm test && npx tsc -b && npm run build)

step "6/7 npm audit 生产依赖"
(cd "$FRONTEND" && npm audit --omit=dev --json > "$CI_OUT/npm-audit-runtime.json" || true)
TOTAL=$(python3 -c "import json;print(json.load(open('$CI_OUT/npm-audit-runtime.json'))['metadata']['vulnerabilities']['total'])")
if [ "$TOTAL" != "0" ]; then
  echo "npm audit 发现 $TOTAL 个生产依赖漏洞，详见 $CI_OUT/npm-audit-runtime.json"
  exit 1
fi
echo "npm audit 通过（0 漏洞）"

# ---------------------------------------------------------------- SBOM 基线
step "7/7 生成依赖清单（SBOM 基线）"
docker run --rm --entrypoint sh "$TEST_IMAGE" -c "pip freeze" > "$CI_OUT/sbom-python.txt"
(cd "$FRONTEND" && npm ls --all --json > "$CI_OUT/sbom-npm.json" 2>/dev/null || true)
echo "已写入 $CI_OUT/sbom-python.txt / $CI_OUT/sbom-npm.json"

# ---------------------------------------------------------------- E2E（可选）
if [ "${SKIP_E2E:-0}" = "1" ]; then
  echo
  echo "SKIP_E2E=1，跳过 Playwright E2E"
else
  step "E2E 冒烟（需要 docker compose 栈在 127.0.0.1:8000 运行）"
  (cd "$FRONTEND" && npx playwright test)
fi

echo
echo "全部门禁通过 ✔"
