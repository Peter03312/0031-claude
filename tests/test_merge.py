"""合片装配测试：隔板相配、漏扫/重扫、分歧、坏掩码、母版不串位。"""

from __future__ import annotations

import pytest

from app.errors import RequestError
from app.merge import parse_request, request_hash, result_for_body

NC = 8


def card(mask: str) -> dict:
    assert len(mask) == NC
    return {"type": "card", "mask": mask}


def sep(label: str) -> dict:
    return {"type": "separator", "label": label}


def body(track1: list[dict], track2: list[dict], nc: int = NC,
         name: str | None = None) -> dict:
    d = {
        "needle_count": nc,
        "tracks": [
            {"name": "scan-A", "items": track1},
            {"name": "scan-B", "items": track2},
        ],
    }
    if name:
        d["name"] = name
    return d


def master_cards(result: dict) -> list[dict]:
    return [e for e in result["master"]["entries"] if e["kind"] == "card"]


# ---------------------------------------------------------------- 正常路径

def test_clean_merge_is_ready_and_publishable() -> None:
    req_body = body(
        [card("10101010"), sep("S1"), card("11110000")],
        [card("10101010"), sep("S1"), card("11110000")],
    )
    _, result = result_for_body(req_body)
    assert result["status"] == "ready"
    assert result["verdict"]["publishable"] is True
    assert result["verdict"]["blockers"] == []
    assert result["rescan"] == []
    cards = master_cards(result)
    assert [c["mask"] for c in cards] == ["10101010", "11110000"]
    assert all(c["status"] == "confirmed" for c in cards)
    assert result["separator_review"]["matched"] == ["S1"]
    assert result["alignment_totals"] == {
        "cost": 0, "gaps": 0, "substitution_cost": 0
    }


def test_input_hash_is_stable_and_order_sensitive() -> None:
    b1 = body([card("10101010")], [card("10101010")])
    b2 = body([card("10101010")], [card("10101010")])
    b3 = body([card("10101010")], [card("10101011")])
    assert request_hash(parse_request(b1)) == request_hash(parse_request(b2))
    assert request_hash(parse_request(b1)) != request_hash(parse_request(b3))


# ---------------------------------------------------------------- 漏扫

def test_missing_card_keeps_later_positions_stable() -> None:
    # 头段：第二路漏第二张；S1 之后的纹板在两条链中身份必须一致
    req_body = body(
        [card("11110000"), card("10101010"), sep("S1"), card("00001111")],
        [card("11110000"), sep("S1"), card("00001111")],
    )
    _, result = result_for_body(req_body)
    assert result["status"] == "blocked"

    rescan = result["rescan"]
    assert len(rescan) == 1
    assert rescan[0]["reason"] == "single_side"
    assert rescan[0]["track"] == 1
    assert rescan[0]["item"] == 2  # 原序列位置，定位到路次/纹板
    assert rescan[0]["pending_needles"] == list(range(1, NC + 1))

    cards = master_cards(result)
    assert [c["mask"] for c in cards] == [
        "11110000",
        "?" * NC,       # 漏张槽位保留挂起，不被后面的纹板顶替
        "00001111",
    ]
    # 关键：S1 之后那张的全局 seq 不因漏张而前移串位
    tail = next(c for c in cards if c["mask"] == "00001111")
    assert tail["segment_label"] == "S1"
    assert tail["card_ref"]["track1"]["item"] == 4
    assert tail["card_ref"]["track2"]["item"] == 3
    assert tail["status"] == "confirmed"


# ---------------------------------------------------------------- 重扫

def test_duplicate_scan_marked_single_side_without_shift() -> None:
    req_body = body(
        [card("11110000"), sep("S1"), card("00001111")],
        [card("11110000"), card("11110000"), sep("S1"), card("00001111")],
    )
    _, result = result_for_body(req_body)
    assert result["status"] == "blocked"
    assert len(result["rescan"]) == 1
    entry = result["rescan"][0]
    assert entry["reason"] == "single_side"
    assert entry["track"] == 2 and entry["item"] == 2
    seg0 = result["segments"][0]
    assert seg0["cost"] == 4 and seg0["gaps"] == 1
    assert [c["mask"] for c in master_cards(result)] == [
        "11110000",
        "?" * NC,
        "00001111",
    ]


# ---------------------------------------------------------------- 灰尘/分歧

def test_dust_disagreement_marks_exact_needles() -> None:
    req_body = body([card("10101010")], [card("10111010")])
    _, result = result_for_body(req_body)
    [entry] = result["rescan"]
    assert entry["reason"] == "mask_disagree"
    assert entry["track"] == 1 and entry["item"] == 1
    assert entry["other_track"] == 2 and entry["other_item"] == 1
    assert entry["pending_needles"] == [4]  # 定位到针位
    [c] = master_cards(result)
    assert c["mask"] == "101?1010"
    assert c["status"] == "pending"
    assert not result["verdict"]["publishable"]


def test_identical_match_cost_is_hamming() -> None:
    req_body = body(
        [card("10101010"), card("00000000")],
        [card("10101010"), card("00000001")],
    )
    _, result = result_for_body(req_body)
    assert result["segments"][0]["cost"] == 1
    assert result["alignment_totals"]["substitution_cost"] == 1


# ---------------------------------------------------------------- 隔板冲突

def test_separator_missing_on_one_track() -> None:
    req_body = body(
        [card("11110000"), sep("S1"), card("00001111")],
        [card("11110000"), card("00001111")],
    )
    _, result = result_for_body(req_body)
    assert result["status"] == "blocked"
    defects = result["separator_review"]["defects"]
    assert len(defects) == 1
    assert defects[0]["reason"] == "separator_missing"
    assert defects[0]["track"] == 1 and defects[0]["item"] == 2
    assert defects[0]["label"] == "S1"
    # 隔板不完整时头段身份无界，保守起见全部段都不对齐、纹板挂起
    assert all(s["aligned"] is False for s in result["segments"])
    # 两路 2+2 张纹板全部进入复扫清单
    assert result["verdict"]["summary"]["single_side_cards"] == 4
    tracks = sorted(
        (e["track"], e["item"]) for e in result["rescan"] if e["reason"] == "single_side"
    )
    assert tracks == [(1, 1), (1, 3), (2, 1), (2, 2)]


def test_separator_prefix_matched_head_still_aligned() -> None:
    # 隔板全部相配，但 S1 之后第 2 路缺一张纹板：属于段内漏扫，尾段照常对齐
    req_body = body(
        [card("11110000"), sep("S1"), card("00001111")],
        [card("11110000"), sep("S1")],
    )
    _, result = result_for_body(req_body)
    seg0, seg1 = result["segments"]
    assert seg0["aligned"] is True and seg0["cost"] == 0
    assert seg1["aligned"] is True
    assert seg1["cost"] == 4 and seg1["gaps"] == 1
    assert result["verdict"]["summary"]["confirmed_cards"] == 1
    assert result["verdict"]["summary"]["single_side_cards"] == 1
    assert not result["verdict"]["publishable"]
    [entry] = result["rescan"]
    assert entry["track"] == 1 and entry["item"] == 3


def test_separator_order_conflict() -> None:
    # 两路都有 S1、S2，但顺序颠倒：应报一次 order 缺陷，指向两张同标号隔板
    req_body = body(
        [sep("S1"), card("11110000"), sep("S2"), card("00001111")],
        [sep("S2"), card("11110000"), sep("S1"), card("00001111")],
    )
    _, result = result_for_body(req_body)
    defects = result["separator_review"]["defects"]
    # 平局裁决选配对序列字典序最小的 S1 作锚点；S2 两侧都未配 → order×2
    orders = [d for d in defects if d["reason"] == "separator_order"]
    assert {d["label"] for d in orders} == {"S2"}
    o = next(d for d in orders if d["track"] == 1)
    assert (o["track"], o["item"]) == (1, 3)
    assert (o["other_track"], o["other_item"]) == (2, 1)
    assert result["separator_review"]["matched"] == ["S1"]
    assert result["separator_review"]["matched_count"] == 1
    assert all(s["aligned"] is False for s in result["segments"])
    assert not result["verdict"]["publishable"]


def test_missing_separator_not_misreported_as_order() -> None:
    # 第 1 路 [S1,S2]、第 2 路 [S2]：S1 仅是第 1 路缺张，
    # 绝不能误报成 S2 顺序冲突，也不能再说 S2 缺失
    req_body = body(
        [sep("S1"), card("11110000"), sep("S2"), card("00001111")],
        [sep("S2"), card("00001111")],
    )
    _, result = result_for_body(req_body)
    defects = result["separator_review"]["defects"]
    assert len(defects) == 1
    d = defects[0]
    assert d["reason"] == "separator_missing"
    assert d["label"] == "S1" and d["track"] == 1 and d["item"] == 1
    assert result["separator_review"]["matched"] == ["S2"]


def test_master_slots_globally_unique_under_conflict() -> None:
    # 隔板冲突后两路纹板分别挂起：segment 与 slot 都不得重号
    req_body = body(
        [sep("S1"), card("11110000"), sep("S2"), card("00001111")],
        [sep("S2"), card("11110000"), sep("S1"), card("00001111")],
    )
    _, result = result_for_body(req_body)
    cards = master_cards(result)
    # slot 全局唯一
    slots = [c["slot"] for c in cards]
    assert len(slots) == len(set(slots))
    # seq 全局唯一且连续
    seqs = [c["seq"] for c in cards]
    assert len(seqs) == len(set(seqs))
    # 同一联合区间内两路纹板共享 region 号，但槽位号不同
    region_slots: dict[int, set[int]] = {}
    for c in cards:
        region_slots.setdefault(c["segment"], set()).add(c["slot"])
    for region, rs in region_slots.items():
        assert len(rs) == sum(1 for c in cards if c["segment"] == region)


@pytest.mark.parametrize(
    "label",
    ["\ud800", "", "   ", 5, None, ["S1"], {"x": 1}, True, 3.2, "x" * 65],
)
def test_malformed_separator_label_is_actionable_400(label) -> None:
    with pytest.raises(RequestError) as ei:
        result_for_body(
            body(
                [{"type": "separator", "label": label}],
                [{"type": "separator", "label": "S1"}],
            )
        )
    err = ei.value
    assert err.code.startswith("separator_label_")
    assert err.track == 1 and err.item == 1


def test_name_field_validation() -> None:
    # 合法 Unicode（含中文、emoji）接受
    for ok in ("链", "🔥", "chain-1907-c"):
        _, result = result_for_body(
            body([card("11110000")], [card("11110000")], name=ok)
        )
        assert result["name"] == ok

    # 非字符串 / 空白 / 不可编码 → 400（构造原始 body 传入）
    for bad in (9, "", "   ", "\ud800"):
        with pytest.raises(RequestError):
            result_for_body(
                {"needle_count": NC, "name": bad, "tracks": [
                    {"items": [card("11110000")]},
                    {"items": [card("11110000")]}]}
            )

    # 缺省 name 字段仍然允许
    _, result = result_for_body(
        {"needle_count": NC, "tracks": [
            {"items": [card("11110000")]},
            {"items": [card("11110000")]}]}
    )
    assert result["name"] is None


# ---------------------------------------------------------------- 坏掩码 / 结构错误

@pytest.mark.parametrize(
    "track1,track2,code,loc",
    [
        # 非法字符（灰尘被原始设备写成别的字符）
        (
            [card("10x01010")],
            [card("10101010")],
            "mask_invalid_char",
            {"track": 1, "item": 1, "needle": 3},
        ),
        # 掩码长度不符（直接构造，夹具本身只接受 8 位掩码）
        (
            [{"type": "card", "mask": "1010"}],
            [card("10101010")],
            "mask_length_invalid",
            {"track": 1, "item": 1},
        ),
        # 第二路的坏掩码（长度 7）
        (
            [card("10101010")],
            [{"type": "card", "mask": "1010101"}],
            "mask_length_invalid",
            {"track": 2, "item": 1},
        ),
        # 隔板缺标号
        (
            [{"type": "separator"}],
            [],
            "separator_label_invalid",
            {"track": 1, "item": 1},
        ),
        # 隔板标号路内重复
        (
            [sep("S1"), sep("S1")],
            [sep("S1"), sep("S1")],
            "separator_label_duplicate",
            {"track": 1, "item": 2, "label": "S1"},
        ),
        # 条目类型非法
        (
            [{"type": "rivet"}],
            [],
            "item_type_invalid",
            {"track": 1, "item": 1},
        ),
    ],
)
def test_bad_requests_are_located(track1, track2, code, loc) -> None:
    with pytest.raises(RequestError) as ei:
        result_for_body(body(track1, track2))
    err = ei.value
    assert err.code == code
    for key, value in loc.items():
        assert getattr(err, key) == value


def test_structure_errors() -> None:
    with pytest.raises(RequestError) as e1:
        parse_request({"needle_count": 8})  # 缺 tracks
    assert e1.value.code == "track_count_invalid"

    with pytest.raises(RequestError) as e2:
        parse_request({"needle_count": 0, "tracks": [[], []]})
    assert e2.value.code == "needle_count_invalid"

    with pytest.raises(RequestError) as e3:
        parse_request({"needle_count": "8", "tracks": [[], []]})
    assert e3.value.code == "needle_count_invalid"


def test_multi_segment_alignment_totals() -> None:
    req_body = body(
        [card("11110000"), sep("S1"), card("10101010"), card("00001111"),
         sep("S2"), card("11001100")],
        [card("11110000"), sep("S1"), card("00001111"),
         sep("S2"), card("11001100")],
    )
    _, result = result_for_body(req_body)
    # 头段干净 0；S1 段漏一张 cost 4；S2 段干净 0
    assert result["alignment_totals"]["cost"] == 4
    assert result["alignment_totals"]["gaps"] == 1
    assert result["separator_review"]["matched"] == ["S1", "S2"]
    labels = [
        e["label"] for e in result["master"]["entries"] if e["kind"] == "separator"
    ]
    assert labels == ["S1", "S2"]
