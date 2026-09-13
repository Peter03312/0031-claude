"""合片任务 HTTP API。

- POST /tasks                创建合片任务（含校验、对齐、裁决、持久化）
- GET  /tasks/{task_id}      读取结果
- POST /tasks/{task_id}/publish  发布可复刻母版（未决针位/隔板缺陷时拒绝）
- GET  /health               存活探针
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import RequestError
from .merge import result_for_body
from . import storage


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.init_db()
    yield


app = FastAPI(
    title="Jacquard Card-Chain Merge Service",
    version="1.0.0",
    description="旧式提花纹板链两路光学扫描的分段单调对齐与母版合片服务",
    lifespan=lifespan,
)


def _task_envelope(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row["result"])
    # 已发布以 published 为准；未发布则呈现域裁决 ready / blocked
    result["status"] = (
        "published" if row["status"] == "published" else result["status"]
    )
    return {
        "task": {
            "id": row["id"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            **result,
        }
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tasks", status_code=201)
async def create_task(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_json",
                    "message": "请求体不是合法 JSON",
                    "location": {},
                }
            },
        )

    try:
        _, result = result_for_body(body)
    except RequestError as exc:
        return JSONResponse(status_code=400, content=exc.to_body())

    task_id = uuid.uuid4().hex[:12]
    created_at = storage.insert_task(task_id, result)

    return JSONResponse(
        status_code=201,
        content={
            "task": {
                "id": task_id,
                "created_at": created_at,
                "published_at": None,
                **result,
            }
        },
    )


@app.get("/tasks/{task_id}")
def read_task(task_id: str) -> JSONResponse:
    row = storage.get_task_row(task_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "task_not_found",
                    "message": f"任务 {task_id} 不存在",
                    "location": {"task_id": task_id},
                }
            },
        )
    return JSONResponse(_task_envelope(row))


@app.post("/tasks/{task_id}/publish")
def publish_task(task_id: str) -> JSONResponse:
    row = storage.get_task_row(task_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "task_not_found",
                    "message": f"任务 {task_id} 不存在",
                    "location": {"task_id": task_id},
                }
            },
        )

    result: dict[str, Any] = row["result"]
    if not result["verdict"]["publishable"]:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "not_publishable",
                    "message": (
                        "隔板不全、顺序冲突或仍有未决针位，禁止发布；"
                        "请先按复扫清单补扫"
                    ),
                    "location": {"task_id": task_id},
                    "blockers": result["verdict"]["blockers"],
                    "rescan": result["rescan"],
                }
            },
        )

    published_at = storage.mark_published(task_id)
    row = storage.get_task_row(task_id)
    assert row is not None
    envelope = _task_envelope(row)
    envelope["published_master"] = {
        "task_id": task_id,
        "input_hash": result["input_hash"],
        "needle_count": result["needle_count"],
        "published_at": published_at,
        "entries": result["master"]["entries"],
    }
    return JSONResponse(envelope)
