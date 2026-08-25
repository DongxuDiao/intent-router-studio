"""统一业务错误：所有 API 错误输出设计文档第 9 节的结构。"""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class NotFoundError(ApiError):
    def __init__(self, resource: str, resource_id: str | None = None) -> None:
        msg = f"{resource} 不存在" + (f": {resource_id}" if resource_id else "")
        super().__init__("NOT_FOUND", msg, 404, {"resource": resource, "id": resource_id})


class ConflictError(ApiError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, 409, details)
