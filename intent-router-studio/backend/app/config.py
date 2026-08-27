"""运行配置：从环境变量 / .env 读取。"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> 仓库根目录（intent-router-studio/）
REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve(p: str | Path) -> Path:
    path = Path(p)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    web_origin: str = "http://127.0.0.1:5173"

    database_url: str = "sqlite:///./var/app.db"
    artifact_root: str = "./var"

    max_upload_mb: int = 100
    max_training_concurrency: int = 1
    hf_home: str = "./var/hf-cache"
    log_raw_text: bool = False

    # 数据限制（设计文档 5.3）
    max_rows_per_file: int = 500_000
    max_text_chars: int = 4_000
    max_batch_inference: int = 1_000
    max_batch_rewrite: int = 100  # 修改方案 §10.4：批量改写上限
    cleanup_min_age_seconds: int = 3600  # 未引用临时文件至少保留 1h，避免误删正在上传的文件

    # XLSX 压缩炸弹防护（V2 §4.4）：解压后总大小 / sheet 数 / 首表行列上限
    max_xlsx_expand_mb: int = 500
    max_xlsx_sheets: int = 20
    max_xlsx_rows: int = 1_000_000
    max_xlsx_cols: int = 256

    # ---- Query 改写（修改方案 §4 / §5.3 / §9.4）----
    rewriter_url: str = "http://127.0.0.1:8010"
    rewrite_timeout_ms: int = 90000
    rewrite_failure_threshold: int = 5      # 连续失败 5 次打开熔断
    rewrite_breaker_open_seconds: float = 30.0
    rewrite_cache_capacity: int = 5_000
    rewrite_cache_ttl_hours: int = 24

    @property
    def artifact_root_path(self) -> Path:
        return _resolve(self.artifact_root)

    @property
    def sqlite_path(self) -> Path:
        """从 database_url 提取 SQLite 文件路径（仅支持 sqlite:/// 前缀）。"""
        url = self.database_url
        prefix = "sqlite:///"
        if not url.startswith(prefix):
            raise ValueError(f"第一版仅支持 SQLite，当前 DATABASE_URL={url}")
        raw = url[len(prefix):]
        if not raw or raw == ":memory:":
            raise ValueError("DATABASE_URL 必须指向具体文件")
        return _resolve(raw)

    @property
    def database_url_absolute(self) -> str:
        return f"sqlite:///{self.sqlite_path}"

    # ---- 制品子目录 ----
    @property
    def uploads_dir(self) -> Path:
        return self.artifact_root_path / "uploads"

    @property
    def projects_dir(self) -> Path:
        return self.artifact_root_path / "projects"

    @property
    def runs_dir(self) -> Path:
        return self.artifact_root_path / "runs"

    @property
    def models_dir(self) -> Path:
        return self.artifact_root_path / "models"

    @property
    def tmp_dir(self) -> Path:
        return self.artifact_root_path / "tmp"

    @property
    def hf_home_path(self) -> Path:
        return _resolve(self.hf_home)

    def ensure_dirs(self) -> None:
        for d in (
            self.artifact_root_path,
            self.sqlite_path.parent,
            self.uploads_dir,
            self.projects_dir,
            self.runs_dir,
            self.models_dir,
            self.tmp_dir,
            self.hf_home_path,
        ):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
