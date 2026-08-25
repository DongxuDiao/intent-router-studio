"""上传链路流式限流（修改方案 V2 §4.4）。

- 分块写入临时文件、增量 SHA-256：完成前服务端不把文件整体读进内存；
- 超过大小限制立即终止并删除临时文件；
- 完成后原子移动到上传目录，Upload 行的哈希/大小与内容一致；
- XLSX 压缩炸弹防护：解压后总大小 / sheet 数 / 首表行列上限；
- 应用层请求体上限：超大 Content-Length 在读 body 前即 413。
"""
from __future__ import annotations

import hashlib
import io

import pandas as pd
import pytest

from app.config import get_settings
from app.errors import ApiError
from app.services import dataset_service


@pytest.fixture
def env_limit(monkeypatch):
    """覆盖大小限制：get_settings 带 lru_cache，改环境变量后必须清缓存才生效。"""

    def _apply(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()

    yield _apply
    get_settings.cache_clear()  # 还原环境后重建缓存，避免污染其他用例


def _csv_bytes(n_rows: int = 5) -> bytes:
    buf = io.StringIO()
    buf.write("text,label,group_id\n")
    for i in range(n_rows):
        buf.write(f"样本 {i},information,g{i}\n")
    return buf.getvalue().encode("utf-8")


def _no_temp_files() -> bool:
    return not list(get_settings().uploads_dir.glob(".tmp-*"))


def test_streaming_upload_roundtrip(db, project_id):
    content = _csv_bytes(20)
    writer = dataset_service.StreamingUploadWriter(db, project_id, "stream.csv", "text/csv")
    try:
        half = len(content) // 2
        writer.write(content[:half])  # 分块写入
        writer.write(content[half:])
        upload = writer.finish(db)
    except Exception:
        writer.abort()
        raise
    assert upload.size_bytes == len(content)
    assert upload.sha256 == hashlib.sha256(content).hexdigest()
    with open(upload.safe_path, "rb") as fh:
        assert fh.read() == content
    assert _no_temp_files()


def test_streaming_upload_exceeds_limit_aborts_and_cleans(db, project_id, env_limit):
    env_limit(MAX_UPLOAD_MB=1)
    writer = dataset_service.StreamingUploadWriter(db, project_id, "big.csv", "text/csv")
    try:
        with pytest.raises(ApiError) as exc:
            writer.write(b"x" * (1024 * 1024 + 1))
        assert exc.value.code == "FILE_TOO_LARGE"
        assert exc.value.status_code == 400
    finally:
        writer.abort()
    # 临时文件已删除；继续写入被拒绝
    assert _no_temp_files()
    with pytest.raises(ApiError):
        writer.write(b"more")


def test_upload_api_streams_and_limits(db, client, project_id, env_limit):
    resp = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("api.csv", io.BytesIO(_csv_bytes(10)), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["size_bytes"] > 0
    assert _no_temp_files()

    # 超限：API 层流式拒绝，不留临时文件、不落 Upload 行
    env_limit(MAX_UPLOAD_MB=1)
    from app.models import Upload

    before = db.query(Upload).count()
    resp = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        files={"file": ("bomb.csv", io.BytesIO(b"a" * (1024 * 1024 + 2)), "text/csv")},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert db.query(Upload).count() == before
    assert _no_temp_files()


def test_request_body_limit_middleware_rejects_before_read(db, client, project_id, env_limit):
    """声明超大的 Content-Length：读 body 前即 413。"""
    env_limit(MAX_UPLOAD_MB=1)
    resp = client.post(
        f"/api/v1/projects/{project_id}/uploads",
        content=b"x",
        headers={"content-length": str(20 * 1024 * 1024)},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"


# ---------------------------------------------------------------- XLSX 防护

def _xlsx_bytes(n_rows: int = 5, n_sheets: int = 1) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        frame = pd.DataFrame({"text": [f"行 {i}" for i in range(n_rows)], "label": ["information"] * n_rows})
        for s in range(n_sheets):
            frame.to_excel(writer, sheet_name=f"sheet{s}", index=False)
    return buf.getvalue()


def _upload_xlsx(db, project_id, content: bytes):
    return dataset_service.save_upload(db, project_id, "guard.xlsx", content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def test_xlsx_normal_file_passes(db, project_id):
    upload = _upload_xlsx(db, project_id, _xlsx_bytes(10))
    frame, used = dataset_service.read_tabular(upload)
    assert used == "binary"
    assert len(frame) == 10


def test_xlsx_expansion_limit(db, project_id, env_limit):
    env_limit(MAX_XLSX_EXPAND_MB=1)
    # 构造解压后超过 1MB 的正常 xlsx（大量互不重复文本压缩率低）
    big = _xlsx_bytes(n_rows=30_000)
    upload = _upload_xlsx(db, project_id, big)
    with pytest.raises(ApiError) as exc:
        dataset_service.read_tabular(upload)
    assert exc.value.code == "ARCHIVE_EXPANSION_TOO_LARGE"


def test_xlsx_sheet_count_limit(db, project_id, env_limit):
    env_limit(MAX_XLSX_SHEETS=2)
    upload = _upload_xlsx(db, project_id, _xlsx_bytes(3, n_sheets=3))
    with pytest.raises(ApiError) as exc:
        dataset_service.read_tabular(upload)
    assert "sheet" in str(exc.value.message)


def test_xlsx_row_limit(db, project_id, env_limit):
    env_limit(MAX_XLSX_ROWS=5)
    upload = _upload_xlsx(db, project_id, _xlsx_bytes(10))
    with pytest.raises(ApiError) as exc:
        dataset_service.read_tabular(upload)
    assert "行数" in str(exc.value.message)


def test_xlsx_not_a_zip(db, project_id):
    upload = _upload_xlsx(db, project_id, b"not a zip at all")
    with pytest.raises(ApiError) as exc:
        dataset_service.read_tabular(upload)
    assert exc.value.code == "PARSE_ERROR"
