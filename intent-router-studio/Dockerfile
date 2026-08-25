# ---------- Stage 1: 构建前端静态资源 ----------
# 基础镜像走可配置前缀（国内网络用镜像源；海外环境用 docker.io）
ARG BASE_REGISTRY=docker.m.daocloud.io
FROM ${BASE_REGISTRY}/library/node:20-alpine AS frontend-builder
WORKDIR /build
# 国内网络可显著加速：--build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=https://registry.npmjs.org
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry ${NPM_REGISTRY} && npm ci
COPY frontend/ ./
RUN npm run build

# ---------- Stage 2: Python 运行时（CPU） ----------
FROM ${BASE_REGISTRY}/library/python:3.11-slim AS runtime

# 国内网络可显著加速：--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    APP_ENV=production \
    APP_HOST=0.0.0.0 \
    ARTIFACT_ROOT=/data/var \
    DATABASE_URL=sqlite:////data/var/app.db \
    HF_HOME=/data/var/hf-cache \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false

# curl 用于 healthcheck；apt 源可切换（国内网络用镜像源）
ARG APT_MIRROR=deb.debian.org
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖走锁文件（修改方案 V2 §5.1）：生产只装运行时闭包，不装 [dev]。
# 锁内 torch==2.9.1 已固定：linux/aarch64 轮子为纯 CPU 构建（其 nvidia
# 依赖都带 platform_machine == "x86_64" 标记），不会拉取 CUDA 包。
COPY backend/requirements.lock /tmp/requirements.lock
RUN pip install --upgrade pip \
    && pip install -r /tmp/requirements.lock

# 后端代码（生产镜像不携带 tests/；应用从 WORKDIR 直接运行，无需安装为包）
COPY backend/app /app/backend/app
COPY backend/alembic /app/backend/alembic
COPY backend/alembic.ini backend/pyproject.toml /app/backend/

# 前端静态资源（main.py 的静态目录候选之一是 REPO_ROOT/frontend/dist）
COPY --from=frontend-builder /build/dist /app/frontend/dist

# 非 root 运行（修改方案 V2 §5.1）：仅数据目录授予写权限
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/var \
    && chown -R app:app /data/var
USER app

EXPOSE 8000

WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------- Stage 3: 测试镜像（修改方案 V2 §5.2） ----------
# 与生产同源（FROM runtime），额外携带 dev 依赖锁、tests、examples、scripts；
# 测试在干净容器内运行，无需挂载宿主代码。注意：本阶段是 Dockerfile 最后一个
# stage，裸 `docker build .` 会构建到它——生产镜像必须显式 --target runtime
# （docker-compose.yml 已固定 target: runtime）：
#   docker build --target test -t intent-router-studio:test .
#   docker run --rm intent-router-studio:test            # pytest -q
FROM runtime AS test
USER root
COPY backend/requirements-dev.lock /tmp/requirements-dev.lock
RUN pip install -r /tmp/requirements-dev.lock
COPY backend/tests /app/backend/tests
COPY examples /app/examples
COPY scripts /app/scripts
USER app
WORKDIR /app/backend
CMD ["python", "-m", "pytest", "tests/", "-q"]
