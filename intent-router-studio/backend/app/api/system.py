"""系统接口（设计文档 9.1）：health / info / config / cleanup。"""
from __future__ import annotations

import platform
import time

import psutil
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.schemas import CleanupRequest
from app.services import inference_service

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/system/info")
def system_info() -> dict:
    settings = get_settings()
    info: dict = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": psutil.cpu_count(logical=True),
        "memory_total_mb": round(psutil.virtual_memory().total / 1024 / 1024),
        "memory_available_mb": round(psutil.virtual_memory().available / 1024 / 1024),
        "artifact_root_free_gb": round(psutil.disk_usage(str(settings.artifact_root_path)).free / 1024**3, 2),
    }
    try:
        import torch

        info["torch"] = torch.__version__
        info["cuda_available"] = torch.cuda.is_available()
        info["mps_available"] = bool(hasattr(torch.backends, "mps") and torch.backends.mps.is_available())
    except Exception as exc:
        info["torch_error"] = str(exc)
    for pkg in ("fastapi", "transformers", "sentence_transformers", "setfit", "sklearn", "pandas", "sqlalchemy"):
        try:
            module = __import__(pkg)
            info[f"version_{pkg}"] = getattr(module, "__version__", "unknown")
        except Exception:
            info[f"version_{pkg}"] = "not-installed"
    return info


@router.get("/system/config")
def system_config() -> dict:
    settings = get_settings()
    return {
        "app_env": settings.app_env,
        "max_upload_mb": settings.max_upload_mb,
        "max_rows_per_file": settings.max_rows_per_file,
        "max_text_chars": settings.max_text_chars,
        "max_batch_inference": settings.max_batch_inference,
        "max_training_concurrency": settings.max_training_concurrency,
        "log_raw_text": settings.log_raw_text,
        "artifact_root_name": settings.artifact_root_path.name,  # 不暴露绝对路径
        "base_model_default": "BAAI/bge-small-zh-v1.5",
    }


@router.post("/system/cleanup")
def cleanup(payload: CleanupRequest, db: Session = Depends(get_db)) -> dict:
    if payload.target == "playground_history":
        removed = inference_service.clear_playground_history(db, payload.project_id)
        return {"removed": removed, "target": payload.target}
    # uploads_tmp：只清理超过安全存活时间的未引用文件。新建 .tmp-*
    # 可能仍由流式上传持有，不得仅因数据库尚无 Upload 行就删除。
    from app.models import Upload

    settings = get_settings()
    referenced = {u.safe_path for u in db.query(Upload).all()}
    removed = 0
    cutoff = time.time() - settings.cleanup_min_age_seconds

    def old_enough(path) -> bool:
        try:
            return path.is_file() and path.stat().st_mtime <= cutoff
        except FileNotFoundError:
            return False

    for path in settings.uploads_dir.iterdir():
        if old_enough(path) and str(path) not in referenced:
            path.unlink(missing_ok=True)
            removed += 1
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    for path in settings.tmp_dir.iterdir():
        if old_enough(path):
            path.unlink(missing_ok=True)
            removed += 1
    return {"removed": removed, "target": payload.target}
