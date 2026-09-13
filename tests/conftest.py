"""测试夹具：每个测试使用独立临时 SQLite，隔离数据卷。"""

from __future__ import annotations

import os
import tempfile

import pytest

# 在导入 app 之前固定 DB_PATH（main 的 startup 会读取它建表）
_TMPDIR = tempfile.mkdtemp(prefix="jacquard-test-")
os.environ["DB_PATH"] = os.path.join(_TMPDIR, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c
