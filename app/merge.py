"""合片：请求校验、隔板相配、分段对齐、母版与复扫清单装配。"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .alignment import align
from .errors import RequestError
from .models import (
    Alignment,
    Item,
    MasterEntry,
    MergeRequest,
    RescanEntry,
    Track,
)

MAX_NEEDLE_COUNT = 10_000
MAX_ITEMS_PER_TRACK = 20_000
MAX_LABEL_LEN = 64
MAX_NAME_LEN = 128


# ---------------------------------------------------------------- 校验与解析

def _err(code: str, message: str, **loc: Any) -> RequestError:
    return RequestError(code, message, **loc)


def _valid_text(value: Any, field: str, *, max_len: int,
                track: int | None = None, item: int | None = None) -> str:
    """文本字段校验：必须是非空、可 UTF-8 编码、长度受限的字符串。

    畸形标签（孤立代理、非字符串、纯空白等）一律转成可操作的 400 输入错误，
    绝不允许逃到哈希/持久化层造成 500。
    """
    loc: dict[str, Any] = {}
    if track is not None:
        loc["track"] = track
    if item is not None:
        loc["item"] = item

    if not isinstance(value, str):
        raise _err(f"{field}_invalid", f"{field} 必须是字符串", **loc)
    if not value.strip():
        raise _err(f"{field}_invalid", f"{field} 不能为空白", **loc)
    if len(value) > max_len:
        raise _err(f"{field}_too_long", f"{field} 长度超过上限 {max_len}", **loc)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _err(
            f"{field}_invalid",
            f"{field} 含不可编码的异常字符（如孤立代理），请检查扫描标签",
            **loc,
        )
    return value


def parse_request(body: Any) -> MergeRequest:
    if not isinstance(body, dict):
        raise _err("invalid_body", "请求体必须是 JSON 对象")

    if "needle_count" not in body:
        raise _err("missing_field", "缺少 needle_count（统一针位数）")
    nc = body["needle_count"]
    if isinstance(nc, bool) or not isinstance(nc, int):
        raise _err("needle_count_invalid", "needle_count 必须是正整数")
    if nc < 1 or nc > MAX_NEEDLE_COUNT:
        raise _err(
            "needle_count_invalid",
            f"needle_count 必须在 1..{MAX_NEEDLE_COUNT} 之间，实际为 {nc}",
        )

    tracks_raw = body.get("tracks")
    if not isinstance(tracks_raw, list) or len(tracks_raw) != 2:
        raise _err(
            "track_count_invalid",
            "tracks 必须是恰好包含两路有序扫描的数组",
        )

    tracks: list[Track] = []
    for t_idx, tr in enumerate(tracks_raw):
        track_no = t_idx + 1
        if not isinstance(tr, dict) or "items" not in tr:
            raise _err(
                "invalid_track",
                f"第 {track_no} 路必须是含 items 数组的对象",
                track=track_no,
            )
        items_raw = tr["items"]
        if not isinstance(items_raw, list):
            raise _err(
                "invalid_track",
                f"第 {track_no} 路 items 必须是数组",
                track=track_no,
            )
        if len(items_raw) > MAX_ITEMS_PER_TRACK:
            raise _err(
                "track_too_long",
                f"第 {track_no} 路条目数超过上限 {MAX_ITEMS_PER_TRACK}",
                track=track_no,
            )

        items: list[Item] = []
        labels: set[str] = set()
        for i_idx, raw in enumerate(items_raw):
            item_no = i_idx + 1
            if not isinstance(raw, dict):
                raise _err(
                    "invalid_item",
                    "每个序列条目必须是对象",
                    track=track_no,
                    item=item_no,
                )
            kind = raw.get("type")
            if kind == "card":
                mask = raw.get("mask")
                if not isinstance(mask, str):
                    raise _err(
                        "mask_invalid",
                        "纹板 mask 必须是 '0'/'1' 字符串",
                        track=track_no,
                        item=item_no,
                    )
                if len(mask) != nc:
                    raise _err(
                        "mask_length_invalid",
                        f"掩码长度 {len(mask)} 与统一针位数 {nc} 不一致",
                        track=track_no,
                        item=item_no,
                    )
                for k, ch in enumerate(mask):
                    if ch not in ("0", "1"):
                        raise _err(
                            "mask_invalid_char",
                            f"掩码第 {k + 1} 针位出现非法字符 {ch!r}（灰尘/坏掩码）",
                            track=track_no,
                            item=item_no,
                            needle=k + 1,
                        )
                items.append(Item(type="card", mask=mask))
            elif kind == "separator":
                label = _valid_text(
                    raw.get("label"),
                    "separator_label",
                    max_len=MAX_LABEL_LEN,
                    track=track_no,
                    item=item_no,
                )
                if label in labels:
                    raise _err(
                        "separator_label_duplicate",
                        f"隔板标号 {label!r} 在该路重复",
                        track=track_no,
                        item=item_no,
                        label=label,
                    )
                labels.add(label)
                items.append(Item(type="separator", label=label))
            else:
                raise _err(
                    "item_type_invalid",
                    "条目 type 只能是 card 或 separator",
                    track=track_no,
                    item=item_no,
                )
        track_name = tr.get("name")
        if track_name is not None:
            track_name = _valid_text(
                track_name, "track_name", max_len=MAX_NAME_LEN, track=track_no
            )
        tracks.append(Track(items=items, name=track_name))

    name = body.get("name")
    if name is not None:
        name = _valid_text(name, "name", max_len=MAX_NAME_LEN)
    return MergeRequest(
        needle_count=nc,
        tracks=(tracks[0], tracks[1]),
        name=name,
    )


def request_hash(req: MergeRequest) -> str:
    """规范化输入的 SHA-256，用于持久化与可复刻校验。"""
    payload: dict[str, Any] = {
        "needle_count": req.needle_count,
        "name": req.name,
        "tracks": [
            {
                "name": tr.name,
                "items": [
                    (
                        {"type": "card", "mask": it.mask}
                        if it.type == "card"
                        else {"type": "separator", "label": it.label}
                    )
                    for it in tr.items
                ],
            }
            for tr in req.tracks
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 隔板相配

def _split_segments(track: Track) -> list[dict[str, Any]]:
    """按隔板切分。段内记录纹板掩码与其在该路的 1 基全局位置。

    segments[k] 对应第 k 张隔板（0 基）之后、第 k+1 张之前；
    segments[0] 为头段，其 label/separator_item 为 None。
    """
    segments: list[dict[str, Any]] = [
        {"label": None, "separator_item": None, "cards": []}
    ]
    for pos, it in enumerate(track.items, start=1):
        if it.type == "separator":
            segments.append(
                {"label": it.label, "separator_item": pos, "cards": []}
            )
        else:
            segments[-1]["cards"].append({"mask": it.mask, "item": pos})
    return segments


def _separator_positions(track: Track) -> list[int]:
    return [
        pos
        for pos, it in enumerate(track.items, start=1)
        if it.type == "separator"
    ]


def _align_separators(
    req: MergeRequest,
) -> tuple[list[tuple[int, int]], list[tuple[str, int | None, int | None]]]:
    """两路隔板的同序相配（最长公共子序列对齐）。

    返回：
      anchors: 相配锚点 ``(第1路隔板0基下标, 第2路隔板0基下标)``，按顺序排列；
      ops:     对齐操作流 ``(动作, 第1路隔板下标|None, 第2路隔板下标|None)``，
               动作为 "match" / "gap1" / "gap2"。

    规则：只有标号相同的隔板可配对；缺一张隔板只产生两个缺口，不强行制造
    “顺序冲突”。裁决与纹板对齐一致：先缺口（未配隔板）最少，平局时配对
    索引序列字典序最小——因此一路漏掉 S1（[S1,S2] 对 [S2]）会相配 S2、
    仅把 S1 判为第 1 路缺张，而不是误报 S2 冲突/缺失。
    """
    s1 = req.tracks[0].separators
    s2 = req.tracks[1].separators
    m, n = len(s1), len(s2)

    # 状态键 = (缺口数, 配对下标序列)；元组字典序天然实现两级裁决
    keys: list[list[tuple[int, tuple[tuple[int, int], ...]] | None]] = [
        [None] * (n + 1) for _ in range(m + 1)
    ]
    moves: list[list[str | None]] = [[None] * (n + 1) for _ in range(m + 1)]
    keys[0][0] = (0, ())

    for r in range(m + 1):
        for c in range(n + 1):
            if r == 0 and c == 0:
                continue
            best = None
            best_move = None

            def consider(cand, move: str) -> None:
                nonlocal best, best_move
                if best is None or cand < best:
                    best, best_move = cand, move

            if r > 0 and keys[r - 1][c] is not None:  # gap1：第1路隔板未配
                pg, pp = keys[r - 1][c]  # type: ignore[misc]
                consider((pg + 1, pp), "gap1")
            if c > 0 and keys[r][c - 1] is not None:  # gap2：第2路隔板未配
                pg, pp = keys[r][c - 1]  # type: ignore[misc]
                consider((pg + 1, pp), "gap2")
            if r > 0 and c > 0 and s1[r - 1].label == s2[c - 1].label:
                pg, pp = keys[r - 1][c - 1]  # type: ignore[misc]
                # match 不增加缺口且延长配对序列；平局时优先（序列更小）
                consider((pg, pp + ((r - 1, c - 1),)), "match")

            keys[r][c] = best
            moves[r][c] = best_move

    ops_rev: list[tuple[str, int | None, int | None]] = []
    r, c = m, n
    while r > 0 or c > 0:
        move = moves[r][c]
        if move == "match":
            ops_rev.append(("match", r - 1, c - 1))
            r -= 1
            c -= 1
        elif move == "gap1":
            ops_rev.append(("gap1", r - 1, None))
            r -= 1
        else:  # gap2
            ops_rev.append(("gap2", None, c - 1))
            c -= 1
    ops = list(reversed(ops_rev))
    anchors = [(a, b) for (op, a, b) in ops if op == "match"]
    return anchors, ops


def _separator_defects(
    req: MergeRequest,
    ops: list[tuple[str, int | None, int | None]],
) -> list[RescanEntry]:
    """根据隔板对齐操作流生成缺陷复扫条目。

    - 同一张标号在两路都出现但未同序相配（两侧都是缺口）→ separator_order，
      指向两路对应隔板，修复目标是“理顺顺序”；
    - 只在一路出现（另一路缺口）→ separator_missing，修复目标是“补隔板”。
    """
    s1 = req.tracks[0].separators
    s2 = req.tracks[1].separators
    pos1 = _separator_positions(req.tracks[0])
    pos2 = _separator_positions(req.tracks[1])

    unmatched1 = {a for (op, a, _) in ops if op == "gap1"}
    unmatched2 = {b for (op, _, b) in ops if op == "gap2"}
    labels1 = {s1[a].label: a for a in unmatched1}
    labels2 = {s2[b].label: b for b in unmatched2}

    defects: list[RescanEntry] = []
    # 同一张标号在两路都存在但未同序相配：两侧各指向真实位置，修复员据此理顺顺序
    for label in sorted(set(labels1) & set(labels2)):
        a, b = labels1[label], labels2[label]
        defects.append(
            RescanEntry(
                reason="separator_order",
                track=1,
                item=pos1[a],
                segment=None,
                segment_label=None,
                label=label,
                other_track=2,
                other_item=pos2[b],
                detail=(
                    f"隔板 {label!r} 在两路都存在但顺序错位："
                    f"第 1 路第 {pos1[a]} 项、第 2 路第 {pos2[b]} 项，需理顺隔板顺序"
                ),
            )
        )
        defects.append(
            RescanEntry(
                reason="separator_order",
                track=2,
                item=pos2[b],
                segment=None,
                segment_label=None,
                label=label,
                other_track=1,
                other_item=pos1[a],
                detail=(
                    f"隔板 {label!r} 在两路都存在但顺序错位："
                    f"第 2 路第 {pos2[b]} 项、第 1 路第 {pos1[a]} 项，需理顺隔板顺序"
                ),
            )
        )
    for label in sorted(set(labels1) - set(labels2)):
        a = labels1[label]
        defects.append(
            RescanEntry(
                reason="separator_missing",
                track=1,
                item=pos1[a],
                segment=None,
                segment_label=None,
                label=label,
                other_track=2,
                detail=f"隔板 {label!r} 仅第 1 路存在：第 2 路漏扫该隔板，需补隔板",
            )
        )
    for label in sorted(set(labels2) - set(labels1)):
        b = labels2[label]
        defects.append(
            RescanEntry(
                reason="separator_missing",
                track=2,
                item=pos2[b],
                segment=None,
                segment_label=None,
                label=label,
                other_track=1,
                detail=f"隔板 {label!r} 仅第 2 路存在：第 1 路漏扫该隔板，需补隔板",
            )
        )

    defects.sort(key=lambda e: (e.track or 0, e.item or 0, e.reason))
    return defects


def _build_regions(
    req: MergeRequest,
    anchors: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    """按相配隔板锚点把两路切成编号全局唯一的联合区间。

    每个锚点之间（及首尾）形成一个区间。只有两侧边界隔板都相配的区间
    （无任何未配隔板落入）才可安全对齐；否则两路该区间内容各自挂起。
    region 编号在整链上连续唯一，母版 ``(segment, slot)`` 因而可被外部系统
    唯一引用——隔板冲突后绝不会再出现重号。
    """
    segs = [_split_segments(req.tracks[0]), _split_segments(req.tracks[1])]

    # 锚点隔板的全局 1 基位置（None 表示链端边界）
    bounds1: list[int | None] = [None]
    bounds2: list[int | None] = [None]
    for a, b in anchors:
        bounds1.append(segs[0][a + 1]["separator_item"])
        bounds2.append(segs[1][b + 1]["separator_item"])
    bounds1.append(None)
    bounds2.append(None)

    sep_pos = [_separator_positions(req.tracks[0]),
               _separator_positions(req.tracks[1])]

    def cards_between(track_no: int, lo_item: int | None,
                      hi_item: int | None) -> list[dict[str, Any]]:
        """取该路全局位置严格落在 (lo, hi) 内的纹板；None 表示链端。"""
        out: list[dict[str, Any]] = []
        for pos, it in enumerate(req.tracks[track_no - 1].items, start=1):
            if it.type != "card":
                continue
            if lo_item is not None and pos <= lo_item:
                continue
            if hi_item is not None and pos >= hi_item:
                continue
            out.append({"mask": it.mask, "item": pos})
        return out

    def unmatched_seps(track_no: int, lo_item: int | None,
                       hi_item: int | None) -> list[dict[str, Any]]:
        out = []
        for p in sep_pos[track_no - 1]:
            if lo_item is not None and p <= lo_item:
                continue
            if hi_item is not None and p >= hi_item:
                continue
            out.append({"item": p,
                        "label": req.tracks[track_no - 1].items[p - 1].label})
        return out

    regions: list[dict[str, Any]] = []
    for k in range(len(anchors) + 1):
        lo1, hi1 = bounds1[k], bounds1[k + 1]
        lo2, hi2 = bounds2[k], bounds2[k + 1]
        u1 = unmatched_seps(1, lo1, hi1)
        u2 = unmatched_seps(2, lo2, hi2)
        label = None
        if k > 0:
            a, _ = anchors[k - 1]
            label = req.tracks[0].separators[a].label
        regions.append(
            {
                "index": k,
                "label": label,  # 区间起始隔板（锚点 anchors[k-1]）；头段为 None
                "aligned": not u1 and not u2,
                "unmatched_separators": {1: u1, 2: u2},
                "cards": {
                    1: cards_between(1, lo1, hi1),
                    2: cards_between(2, lo2, hi2),
                },
            }
        )
    return regions


# ---------------------------------------------------------------- 序列化辅助

def _entry_dict(e: RescanEntry) -> dict[str, Any]:
    d: dict[str, Any] = {
        "reason": e.reason,
        "track": e.track,
        "item": e.item,
        "segment": e.segment,
        "segment_label": e.segment_label,
    }
    if e.label is not None:
        d["label"] = e.label
    if e.other_track is not None:
        d["other_track"] = e.other_track
    if e.other_item is not None:
        d["other_item"] = e.other_item
    if e.pending_needles:
        d["pending_needles"] = e.pending_needles
    if e.card_ref:
        d["card_ref"] = e.card_ref
    if e.detail:
        d["detail"] = e.detail
    return d


def _master_dict(e: MasterEntry) -> dict[str, Any]:
    d: dict[str, Any] = {
        "seq": e.seq,
        "segment": e.segment,
        "segment_label": e.segment_label,
        "slot": e.slot,
        "kind": e.kind,
    }
    if e.kind == "separator":
        d["label"] = e.label
        return d
    d["source"] = e.source
    d["mask"] = e.mask
    d["pending_needles"] = e.pending_needles
    d["status"] = "confirmed" if not e.pending_needles else "pending"
    d["card_ref"] = e.card_ref
    return d


# ---------------------------------------------------------------- 合片装配

def build_result(req: MergeRequest) -> dict[str, Any]:
    nc = req.needle_count
    all_needles = list(range(1, nc + 1))

    # 隔板同序相配（LCS 锚点）→ 缺陷裁决 → 全局唯一编号的联合区间
    anchors, sep_ops = _align_separators(req)
    sep_defects = _separator_defects(req, sep_ops)
    regions = _build_regions(req, anchors)
    matched_labels = [
        req.tracks[0].separators[a].label for a, _ in anchors
    ]

    master_entries: list[MasterEntry] = []
    rescans: list[RescanEntry] = list(sep_defects)
    segment_payloads: list[dict[str, Any]] = []
    total_cost = 0
    total_gaps = 0
    total_subst = 0
    confirmed_cards = 0
    disagree_cards = 0
    single_cards = 0
    aligned_segments = 0
    seq = 1   # 母版全局条目号（隔板与纹板统一计数）
    slot_no = 0  # 纹板槽位全局唯一号（隔板不占号），外部系统据此唯一引用

    def add_card_slot(
        region_index: int,
        seg_label: str | None,
        source: str,
        mask: str,
        pending: list[int],
        ref: dict[str, Any],
    ) -> None:
        nonlocal seq, slot_no, confirmed_cards
        slot_no += 1
        master_entries.append(
            MasterEntry(
                seq=seq,
                segment=region_index,
                segment_label=seg_label,
                slot=slot_no,  # 全局唯一：隔板冲突后也绝不重号
                kind="card",
                mask=mask,
                source=source,
                card_ref=ref,
                pending_needles=pending,
            )
        )
        if source == "match" and not pending:
            confirmed_cards += 1
        seq += 1

    def add_separator_slot(label: str, region_index: int) -> None:
        nonlocal seq
        master_entries.append(
            MasterEntry(
                seq=seq,
                segment=region_index,
                segment_label=label,
                slot=0,
                kind="separator",
                label=label,
            )
        )
        seq += 1

    def cards_payload(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"segment_index": k, "item": c["item"], "mask": c["mask"]}
            for k, c in enumerate(cards)
        ]

    def dump_unaligned(track_no: int, region: dict[str, Any],
                       cards: list[dict[str, Any]]) -> None:
        """未配隔板区间：不对齐，逐张挂起；槽位号全局唯一、不与别路撞号。"""
        nonlocal single_cards
        for card in cards:
            single_cards += 1
            ref = {"track": track_no, "item": card["item"]}
            rescans.append(
                RescanEntry(
                    reason="single_side",
                    track=track_no,
                    item=card["item"],
                    segment=region["index"],
                    segment_label=region["label"],
                    pending_needles=list(all_needles),
                    card_ref=ref,
                    detail="隔板不全或顺序冲突，该纹板未参与对齐，需复扫",
                )
            )
            add_card_slot(
                region["index"], region["label"], f"track{track_no}",
                "?" * nc, list(all_needles), ref,
            )

    for region in regions:
        idx = region["index"]
        label = region["label"]

        # 本区间起始的相配隔板（头段无）先进入母版，身份固定在两段之间
        if idx > 0:
            add_separator_slot(matched_labels[idx - 1], idx)

        c1 = region["cards"][1]
        c2 = region["cards"][2]

        if not region["aligned"]:
            # 两路各自挂起（同 region 号、槽位号全局唯一，不再重号）
            dump_unaligned(1, region, c1)
            dump_unaligned(2, region, c2)
            segment_payloads.append(
                {
                    "index": idx,
                    "label": label,
                    "aligned": False,
                    "track1_cards": cards_payload(c1),
                    "track2_cards": cards_payload(c2),
                    "unmatched_separators": region["unmatched_separators"],
                    "cost": None,
                    "gaps": None,
                    "pairs": [],
                    "ops": [],
                    "note": "区间内存在未配隔板（缺张或顺序错位），未对齐，全部纹板挂起待复扫",
                }
            )
            continue

        aligned_segments += 1
        alignment: Alignment = align(
            [c["mask"] for c in c1], [c["mask"] for c in c2]
        )
        total_cost += alignment.cost
        total_gaps += alignment.gaps

        op_payloads: list[dict[str, Any]] = []
        subst_cost = 0
        for op in alignment.ops:
            op_d: dict[str, Any] = {"op": op.op, "cost": op.cost}
            if op.op == "match":
                i, j = op.i, op.j
                x1, x2 = c1[i], c2[j]
                m1, m2 = x1["mask"], x2["mask"]
                subst_cost += op.cost
                total_subst += op.cost
                pending = [k + 1 for k in range(nc) if m1[k] != m2[k]]
                consensus = "".join(
                    m1[k] if m1[k] == m2[k] else "?" for k in range(nc)
                )
                ref = {
                    "track1": {"item": x1["item"], "segment_index": i},
                    "track2": {"item": x2["item"], "segment_index": j},
                }
                if pending:
                    disagree_cards += 1
                    rescans.append(
                        RescanEntry(
                            reason="mask_disagree",
                            track=1,
                            item=x1["item"],
                            segment=idx,
                            segment_label=label,
                            other_track=2,
                            other_item=x2["item"],
                            pending_needles=pending,
                            card_ref=ref,
                            detail=(
                                f"两路纹板在 {len(pending)} 个针位上分歧"
                                f"（汉明距离 {op.cost}）"
                            ),
                        )
                    )
                add_card_slot(idx, label, "match", consensus, pending, ref)
                op_d["track1"] = {"item": x1["item"], "segment_index": i}
                op_d["track2"] = {"item": x2["item"], "segment_index": j}
                op_d["pending_needles"] = pending
            elif op.op == "delete":
                i = op.i
                x1 = c1[i]
                single_cards += 1
                ref = {"track": 1, "item": x1["item"], "segment_index": i}
                rescans.append(
                    RescanEntry(
                        reason="single_side",
                        track=1,
                        item=x1["item"],
                        segment=idx,
                        segment_label=label,
                        pending_needles=list(all_needles),
                        card_ref=ref,
                        detail="仅第 1 路存在：第 2 路漏扫或本路回带重扫，需复扫裁决",
                    )
                )
                add_card_slot(
                    idx, label, "track1", "?" * nc, list(all_needles), ref
                )
                op_d["track1"] = {"item": x1["item"], "segment_index": i}
            else:  # insert
                j = op.j
                x2 = c2[j]
                single_cards += 1
                ref = {"track": 2, "item": x2["item"], "segment_index": j}
                rescans.append(
                    RescanEntry(
                        reason="single_side",
                        track=2,
                        item=x2["item"],
                        segment=idx,
                        segment_label=label,
                        pending_needles=list(all_needles),
                        card_ref=ref,
                        detail="仅第 2 路存在：第 1 路漏扫或本路回带重扫，需复扫裁决",
                    )
                )
                add_card_slot(
                    idx, label, "track2", "?" * nc, list(all_needles), ref
                )
                op_d["track2"] = {"item": x2["item"], "segment_index": j}
            op_payloads.append(op_d)

        segment_payloads.append(
            {
                "index": idx,
                "label": label,
                "aligned": True,
                "track1_cards": cards_payload(c1),
                "track2_cards": cards_payload(c2),
                "cost": alignment.cost,
                "gaps": alignment.gaps,
                "substitution_cost": subst_cost,
                "match_count": len(alignment.pairs),
                "pairs": [[i, j] for i, j in alignment.pairs],
                "ops": op_payloads,
            }
        )

    pending_needle_total = sum(len(e.pending_needles) for e in rescans)
    blockers: list[dict[str, Any]] = [
        {"code": e.reason, "message": e.detail, "location": _entry_dict(e)}
        for e in sep_defects
    ]
    remaining = len(rescans) - len(sep_defects)
    if remaining > 0:
        blockers.append(
            {
                "code": "unresolved_cards",
                "message": f"仍有 {remaining} 条纹板含未决针位，禁止发布",
            }
        )

    publishable = not rescans
    status = "ready" if publishable else "blocked"

    return {
        "name": req.name,
        "needle_count": nc,
        "input_hash": request_hash(req),
        "status": status,
        "tracks": [
            {
                "name": tr.name,
                "item_count": len(tr.items),
                "card_count": len(tr.cards),
                "separator_count": len(tr.separators),
            }
            for tr in req.tracks
        ],
        "separator_review": {
            "matched": matched_labels,
            "matched_count": len(anchors),
            "defects": [_entry_dict(d) for d in sep_defects],
        },
        "segments": segment_payloads,
        "alignment_totals": {
            "cost": total_cost,
            "gaps": total_gaps,
            "substitution_cost": total_subst,
        },
        "master": {
            "needle_count": nc,
            "entries": [_master_dict(e) for e in master_entries],
        },
        "rescan": [_entry_dict(e) for e in rescans],
        "verdict": {
            "publishable": publishable,
            "blockers": blockers,
            "summary": {
                "segments": len(segment_payloads),
                "aligned_segments": aligned_segments,
                "confirmed_cards": confirmed_cards,
                "disagreeing_cards": disagree_cards,
                "single_side_cards": single_cards,
                "pending_needles": pending_needle_total,
                "total_cost": total_cost,
                "total_gaps": total_gaps,
            },
        },
    }


def result_for_body(body: Any) -> tuple[MergeRequest, dict[str, Any]]:
    req = parse_request(body)
    # 深拷贝隔离：持久化与响应互不影响
    return req, copy.deepcopy(build_result(req))
