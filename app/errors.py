"""Domain error: rendered as ``{"error": {"code", "message", ...}}``."""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.extra:
            body["error"]["details"] = self.extra
        return body


def not_found(kind: str, identifier: Any) -> ApiError:
    return ApiError(404, f"{kind}_not_found", f"{kind} not found", **{kind: identifier})
