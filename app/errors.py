"""错误类型与错误定位。

所有面向用户的错误位置均采用 1 基下标：
- track:  1 或 2，表示第一路 / 第二路扫描；
- item:   该路有序序列中的纹板/隔板位置；
- needle: 统一针位序号，从 1 开始。
"""

from __future__ import annotations

from typing import Any


class RequestError(Exception):
    """合片请求本身的可定位错误（HTTP 400）。"""

    def __init__(
        self,
        code: str,
        message: str,
        track: int | None = None,
        item: int | None = None,
        needle: int | None = None,
        label: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.track = track
        self.item = item
        self.needle = needle
        self.label = label

    def to_body(self) -> dict[str, Any]:
        location: dict[str, int | str] = {}
        if self.track is not None:
            location["track"] = self.track
        if self.item is not None:
            location["item"] = self.item
        if self.needle is not None:
            location["needle"] = self.needle
        if self.label is not None:
            location["label"] = self.label
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "location": location,
            }
        }


class TaskNotFound(Exception):
    """任务不存在（HTTP 404）。"""

    def __init__(self, task_id: str) -> None:
        super().__init__(f"task not found: {task_id}")
        self.task_id = task_id
