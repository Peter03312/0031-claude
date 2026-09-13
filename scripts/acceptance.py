#!/usr/bin/env python3
"""端到端验收脚本（verify 一次性服务使用，走真实 HTTP 接口）。

由 docker compose 的 verify 服务在 api 健康后执行；
退出码非 0 即验收失败。覆盖：正常合片、漏扫、重扫、灰尘分歧、
隔板缺张、隔板顺序冲突、坏掩码定位、发布门控与幂等。
"""

from __future__ import annotations

import json
import os
import sys

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")
NC = 8

failures: list[str] = []


def card(mask: str) -> dict:
    assert len(mask) == NC
    return {"type": "card", "mask": mask}


def sep(label: str) -> dict:
    return {"type": "separator", "label": label}


def body(t1: list[dict], t2: list[dict]) -> dict:
    return {
        "needle_count": NC,
        "tracks": [
            {"name": "scan-A", "items": t1},
            {"name": "scan-B", "items": t2},
        ],
    }


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def create(client: httpx.Client, payload: dict) -> tuple[int, dict]:
    r = client.post("/tasks", json=payload)
    return r.status_code, r.json()


def main() -> int:
    client = httpx.Client(base_url=BASE_URL, timeout=10)

    print(f"acceptance against {BASE_URL}")
    h = client.get("/health")
    check("health 200", h.status_code == 200 and h.json()["status"] == "ok")

    # 1. 正常合片 → 可发布
    code, resp = create(
        client,
        body(
            [card("10101010"), sep("S1"), card("11110000")],
            [card("10101010"), sep("S1"), card("11110000")],
        ),
    )
    check("clean create 201", code == 201)
    tid = resp["task"]["id"]
    check("clean status ready", resp["task"]["status"] == "ready")
    check("clean zero cost", resp["task"]["alignment_totals"]["cost"] == 0)
    check("hash persisted", len(resp["task"]["input_hash"]) == 64)
    got = client.get(f"/tasks/{tid}")
    check("read-back identical", got.status_code == 200 and
          got.json()["task"]["input_hash"] == resp["task"]["input_hash"])
    pub = client.post(f"/tasks/{tid}/publish")
    check("publish ready 200", pub.status_code == 200)
    pub2 = client.post(f"/tasks/{tid}/publish")
    check("publish idempotent", pub2.status_code == 200 and
          pub2.json()["published_master"]["published_at"]
          == pub.json()["published_master"]["published_at"])

    # 2. 漏扫：缺口保留挂起槽位，后续不串位
    code, resp = create(
        client,
        body(
            [card("11110000"), card("10101010"), sep("S1"), card("00001111")],
            [card("11110000"), sep("S1"), card("00001111")],
        ),
    )
    t = resp["task"]
    cards = [e for e in t["master"]["entries"] if e["kind"] == "card"]
    masks = [e["mask"] for e in cards]
    check("missing: blocked", t["status"] == "blocked")
    check("missing: gap slot kept", masks == ["11110000", "?" * NC, "00001111"],
          json.dumps(masks))
    check("missing: rescan located",
          t["rescan"][0]["track"] == 1 and t["rescan"][0]["item"] == 2)
    check("missing: publish 409",
          client.post(f"/tasks/{t['id']}/publish").status_code == 409)

    # 3. 重扫：副本挂起，后续不错位
    code, resp = create(
        client,
        body(
            [card("11110000"), sep("S1"), card("00001111")],
            [card("11110000"), card("11110000"), sep("S1"), card("00001111")],
        ),
    )
    t = resp["task"]
    e = t["rescan"][0]
    check("duplicate: single_side on track2 item2",
          e["reason"] == "single_side" and e["track"] == 2 and e["item"] == 2)
    check("duplicate: tail stable",
          [x["mask"] for x in t["master"]["entries"]
           if x["kind"] == "card"][-1] == "00001111")

    # 4. 灰尘分歧：精确定位针位
    code, resp = create(
        client, body([card("10101010")], [card("10111010")])
    )
    t = resp["task"]
    check("dust: pending needle 4",
          t["rescan"][0]["pending_needles"] == [4])
    check("dust: consensus has ?",
          [e for e in t["master"]["entries"]
           if e["kind"] == "card"][0]["mask"] == "101?1010")

    # 5. 隔板缺张
    code, resp = create(
        client,
        body(
            [card("11110000"), sep("S1"), card("00001111")],
            [card("11110000"), card("00001111")],
        ),
    )
    t = resp["task"]
    d = t["separator_review"]["defects"][0]
    check("separator missing: located",
          d["reason"] == "separator_missing" and d["track"] == 1
          and d["item"] == 2 and d["label"] == "S1")
    check("separator missing: publish 409",
          client.post(f"/tasks/{t['id']}/publish").status_code == 409)

    # 6. 隔板顺序冲突
    code, resp = create(
        client,
        body(
            [sep("S1"), card("11110000"), sep("S2")],
            [sep("S2"), card("11110000"), sep("S1")],
        ),
    )
    t = resp["task"]
    reasons = {d["reason"] for d in t["separator_review"]["defects"]}
    check("separator order: conflict detected", "separator_order" in reasons)
    check("separator order: no aligned segments",
          t["verdict"]["summary"]["aligned_segments"] == 0)

    # 7. 坏掩码 → 400，定位到路次/纹板/针位
    r = client.post("/tasks", json=body([card("10x01010")], [card("10101010")]))
    check("bad mask: 400", r.status_code == 400)
    loc = r.json()["error"]["location"]
    check("bad mask: needle located",
          loc == {"track": 1, "item": 1, "needle": 3}, json.dumps(loc))

    # 8. 未知任务
    check("unknown task 404", client.get("/tasks/nope").status_code == 404)

    if failures:
        print(f"\nacceptance FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("\nacceptance PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
