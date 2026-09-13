"""HTTP 接口测试：创建、读取、发布裁决、错误定位与持久化。"""

from __future__ import annotations

NC = 8


def card(mask: str) -> dict:
    return {"type": "card", "mask": mask}


def sep(label: str) -> dict:
    return {"type": "separator", "label": label}


def make_body(track1: list[dict], track2: list[dict]) -> dict:
    return {
        "needle_count": NC,
        "tracks": [
            {"name": "A", "items": track1},
            {"name": "B", "items": track2},
        ],
    }


def test_health(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_create_and_get_persists_hash_mapping_cost(client) -> None:
    payload = make_body(
        [card("10101010"), sep("S1"), card("11110000")],
        [card("10101010"), sep("S1"), card("11110000")],
    )
    r = client.post("/tasks", json=payload)
    assert r.status_code == 201
    created = r.json()["task"]
    task_id = created["id"]
    assert len(created["input_hash"]) == 64
    assert created["status"] == "ready"
    assert created["alignment_totals"]["cost"] == 0

    r2 = client.get(f"/tasks/{task_id}")
    assert r2.status_code == 200
    fetched = r2.json()["task"]
    assert fetched["input_hash"] == created["input_hash"]
    assert fetched["segments"] == created["segments"]
    assert fetched["master"]["entries"][0]["mask"] == "10101010"


def test_publish_ready_task_returns_master(client) -> None:
    r = client.post(
        "/tasks",
        json=make_body([card("10101010")], [card("10101010")]),
    )
    task_id = r.json()["task"]["id"]

    pr = client.post(f"/tasks/{task_id}/publish")
    assert pr.status_code == 200
    body = pr.json()
    assert body["task"]["status"] == "published"
    assert body["published_master"]["input_hash"] == r.json()["task"]["input_hash"]
    assert body["published_master"]["entries"][0]["mask"] == "10101010"

    # 幂等：再次发布仍 200，且发布时间不变
    pr2 = client.post(f"/tasks/{task_id}/publish")
    assert pr2.status_code == 200
    assert (
        pr2.json()["published_master"]["published_at"]
        == body["published_master"]["published_at"]
    )


def test_publish_blocked_task_is_409_with_locations(client) -> None:
    # 漏扫 + 分歧同时存在
    payload = make_body(
        [card("10101010"), card("11110000"), sep("S1"), card("00001111")],
        [card("10101010"), sep("S1"), card("00001110")],
    )
    task_id = client.post("/tasks", json=payload).json()["task"]["id"]

    pr = client.post(f"/tasks/{task_id}/publish")
    assert pr.status_code == 409
    err = pr.json()["error"]
    assert err["code"] == "not_publishable"
    assert err["location"]["task_id"] == task_id
    reasons = {e["reason"] for e in err["rescan"]}
    assert reasons == {"single_side", "mask_disagree"}
    disagree = next(e for e in err["rescan"] if e["reason"] == "mask_disagree")
    assert disagree["pending_needles"] == [8]

    # 未发布任务读取时状态仍为 blocked
    gr = client.get(f"/tasks/{task_id}").json()["task"]
    assert gr["status"] == "blocked"
    assert gr["verdict"]["publishable"] is False


def test_separator_conflict_create_succeeds_but_publish_denied(client) -> None:
    payload = make_body(
        [sep("S1"), card("11110000")],
        [sep("S2"), card("11110000")],
    )
    r = client.post("/tasks", json=payload)
    assert r.status_code == 201
    task = r.json()["task"]
    assert task["status"] == "blocked"
    # 两路隔板标号互不相同（S1 vs S2）→ 各自缺失，修复目标是补隔板
    codes = {b["code"] for b in task["verdict"]["blockers"]}
    assert "separator_missing" in codes
    task_id = task["id"]
    pr = client.post(f"/tasks/{task_id}/publish")
    assert pr.status_code == 409


def test_separator_order_conflict_points_at_real_positions(client) -> None:
    # S1、S2 两路都在但顺序颠倒 → order 定位到每张隔板的真实位置
    payload = make_body(
        [sep("S1"), card("11110000"), sep("S2"), card("00001111")],
        [sep("S2"), card("11110000"), sep("S1"), card("00001111")],
    )
    r = client.post("/tasks", json=payload)
    assert r.status_code == 201
    task = r.json()["task"]
    orders = [
        d for d in task["separator_review"]["defects"]
        if d["reason"] == "separator_order"
    ]
    # 平局裁决选 S1 作锚点；S2 两侧都未配 → order×2，定位到 S2 真实位置
    assert {d["label"] for d in orders} == {"S2"}
    o = next(d for d in orders if d["track"] == 1)
    assert (o["track"], o["item"]) == (1, 3)
    assert (o["other_track"], o["other_item"]) == (2, 1)
    assert task["separator_review"]["matched"] == ["S1"]
    assert client.post(f"/tasks/{task['id']}/publish").status_code == 409


def test_missing_separator_not_misreported_via_api(client) -> None:
    # 一路漏掉 S1：只能报 S1 缺失，不得报 S2 冲突
    payload = make_body(
        [sep("S1"), card("11110000"), sep("S2"), card("00001111")],
        [sep("S2"), card("00001111")],
    )
    task = client.post("/tasks", json=payload).json()["task"]
    defects = task["separator_review"]["defects"]
    assert len(defects) == 1
    assert defects[0]["reason"] == "separator_missing"
    assert defects[0]["label"] == "S1"
    assert task["separator_review"]["matched"] == ["S2"]


def test_master_slots_globally_unique_via_api(client) -> None:
    payload = make_body(
        [sep("S1"), card("11110000"), sep("S2"), card("00001111")],
        [sep("S2"), card("11110000"), sep("S1"), card("00001111")],
    )
    task = client.post("/tasks", json=payload).json()["task"]
    card_entries = [
        e for e in task["master"]["entries"] if e["kind"] == "card"
    ]
    slots = [e["slot"] for e in card_entries]
    assert len(slots) == len(set(slots)), "母版纹板槽位号必须全局唯一"
    pairs = [(e["segment"], e["slot"]) for e in card_entries]
    assert len(pairs) == len(set(pairs))


def test_malformed_label_returns_400_not_500(client) -> None:
    # 孤立代理字符（畸形扫描标签）必须是可操作的 400。
    # 用 JSON 转义 \\ud800 的原始字节发送（合法 JSON 文本、解码后是孤立代理），
    # 以真正测到服务端而不是客户端编码阶段。
    raw = (
        b'{"needle_count": 8, "tracks": ['
        b'{"items": [{"type": "separator", "label": "bad\\ud800label"},'
        b' {"type": "card", "mask": "11110000"}]},'
        b'{"items": [{"type": "separator", "label": "S1"},'
        b' {"type": "card", "mask": "11110000"}]}]}'
    )
    r = client.post(
        "/tasks", content=raw, headers={"content-type": "application/json"}
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"].startswith("separator_label_")
    assert err["location"]["track"] == 1 and err["location"]["item"] == 1


def test_bad_mask_returns_400_with_needle_location(client) -> None:
    payload = make_body([card("10x01010")], [card("10101010")])
    r = client.post("/tasks", json=payload)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "mask_invalid_char"
    assert err["location"] == {"track": 1, "item": 1, "needle": 3}


def test_malformed_json_400(client) -> None:
    r = client.post(
        "/tasks",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_json"


def test_task_not_found_404(client) -> None:
    r = client.get("/tasks/doesnotexist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "task_not_found"

    pr = client.post("/tasks/doesnotexist/publish")
    assert pr.status_code == 404


def test_missing_card_scenarios_end_to_end_no_shift(client) -> None:
    """漏扫场景：补扫后重新创建任务，母版尾部身份与之前一致。"""
    with_gap = make_body(
        [card("11110000"), card("10101010"), sep("S1"), card("00001111")],
        [card("11110000"), sep("S1"), card("00001111")],
    )
    t1 = client.post("/tasks", json=with_gap).json()["task"]
    blocked_masks = [
        e["mask"] for e in t1["master"]["entries"] if e["kind"] == "card"
    ]
    assert blocked_masks == ["11110000", "?" * NC, "00001111"]
    assert client.post(f"/tasks/{t1['id']}/publish").status_code == 409

    # 修复员补扫第二路漏掉的 10101010 后重新提交
    repaired = make_body(
        [card("11110000"), card("10101010"), sep("S1"), card("00001111")],
        [card("11110000"), card("10101010"), sep("S1"), card("00001111")],
    )
    t2 = client.post("/tasks", json=repaired).json()["task"]
    clean_masks = [
        e["mask"] for e in t2["master"]["entries"] if e["kind"] == "card"
    ]
    assert clean_masks == ["11110000", "10101010", "00001111"]
    assert t2["status"] == "ready"
    pr = client.post(f"/tasks/{t2['id']}/publish")
    assert pr.status_code == 200
    published = pr.json()["published_master"]["entries"]
    assert [e["mask"] for e in published if e["kind"] == "card"] == clean_masks
    # 尾部纹板在修复前后引用的是同一张物理纹板（第 1 路 item=4），身份未串
    assert clean_masks[-1:] == ["00001111"]
