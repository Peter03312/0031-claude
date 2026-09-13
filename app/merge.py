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


# ---------------------------------------------------------------- 校验与解析

def _err(code: str, message: str, **loc: Any) -> RequestError:
    return RequestError(code, message, **loc)


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
                label = raw.get("label")
                if not isinstance(label, str) or not label:
                    raise _err(
                        "separator_label_invalid",
                        "隔板必须带非空唯一 label",
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
        tracks.append(Track(items=items, name=tr.get("name")))

    name = body.get("name")
    return MergeRequest(
        needle_count=nc,
        tracks=(tracks[0], tracks[1]),
        name=name if isinstance(name, str) else None,
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


def _analyze_separators(
    req: MergeRequest,
) -> tuple[list[str], list[RescanEntry], int]:
    """返回 (同序相配的标号前缀, 隔板缺陷复扫条目, 相配隔板数 p)。

    两路隔板必须同序且强制相配：逐位比较标号，首个不一致点即顺序冲突；
    其后所有隔板（含冲突点两侧）均无法相配，按缺张挂起。
    """
    s1 = req.tracks[0].separators
    s2 = req.tracks[1].separators
    pos1 = _separator_positions(req.tracks[0])
    pos2 = _separator_positions(req.tracks[1])

    p = 0
    limit = min(len(s1), len(s2))
    while p < limit and s1[p].label == s2[p].label:
        p += 1

    defects: list[RescanEntry] = []

    if p < limit:
        defects.append(
            RescanEntry(
                reason="separator_order",
                track=1,
                item=pos1[p],
                segment=None,
                segment_label=None,
                label=s1[p].label,
                other_track=2,
                other_item=pos2[p],
                detail=(
                    f"隔板顺序冲突：第 1 路此处为 {s1[p].label!r}，"
                    f"第 2 路此处为 {s2[p].label!r}"
                ),
            )
        )

    # 冲突点之后（含冲突点）的隔板一律无法相配；仅缺张时从长度差处开始
    for k in range(p + (1 if p < limit else 0), len(s1)):
        defects.append(
            RescanEntry(
                reason="separator_missing",
                track=1,
                item=pos1[k],
                segment=None,
                segment_label=None,
                label=s1[k].label,
                other_track=2,
                detail=f"隔板 {s1[k].label!r} 在第 2 路缺张或位置不可配",
            )
        )
    for k in range(p + (1 if p < limit else 0), len(s2)):
        defects.append(
            RescanEntry(
                reason="separator_missing",
                track=2,
                item=pos2[k],
                segment=None,
                segment_label=None,
                label=s2[k].label,
                other_track=1,
                detail=f"隔板 {s2[k].label!r} 在第 1 路缺张或位置不可配",
            )
        )

    return [s1[k].label for k in range(p)], defects, p


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

    matched_labels, sep_defects, p = _analyze_separators(req)
    segs = [_split_segments(req.tracks[0]), _split_segments(req.tracks[1])]

    # 可安全对齐的段：
    # - 段 0..p-1：两侧均由相配隔板围成（头段由第一张相配隔板封尾）；
    # - 最后一张相配隔板之后的尾段，仅当两路隔板恰好全部相配（无冲突点、
    #   无缺张、无多余隔板）时才可对齐；否则尾段身份无界，挂起待复扫。
    n_sep = [len(req.tracks[0].separators), len(req.tracks[1].separators)]
    order_conflict = len(sep_defects) > 0 and sep_defects[0].reason == "separator_order"
    tail_aligned = (not order_conflict) and n_sep[0] == p and n_sep[1] == p
    alignable = p + (1 if tail_aligned else 0)

    master_entries: list[MasterEntry] = []
    rescans: list[RescanEntry] = list(sep_defects)
    segment_payloads: list[dict[str, Any]] = []
    total_cost = 0
    total_gaps = 0
    total_subst = 0
    confirmed_cards = 0
    disagree_cards = 0
    single_cards = 0
    seq = 1  # 母版全局槽位序号（隔板占号；缺张保留挂起槽位，绝不串位）

    def add_card_slot(
        seg_index: int,
        seg_label: str | None,
        slot: int,
        source: str,
        mask: str,
        pending: list[int],
        ref: dict[str, Any],
    ) -> None:
        nonlocal seq, confirmed_cards
        master_entries.append(
            MasterEntry(
                seq=seq,
                segment=seg_index,
                segment_label=seg_label,
                slot=slot,
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

    def add_separator_slot(label: str, seg_index: int) -> None:
        nonlocal seq
        master_entries.append(
            MasterEntry(
                seq=seq,
                segment=seg_index,
                segment_label=label,
                slot=0,
                kind="separator",
                label=label,
            )
        )
        seq += 1

    def cards_payload(seg: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"segment_index": k, "item": c["item"], "mask": c["mask"]}
            for k, c in enumerate(seg["cards"])
        ]

    def dump_unaligned(
        track_no: int, seg: dict[str, Any], seg_index: int
    ) -> None:
        """隔板结构破损后的剩余段：不对齐，逐张挂起，保留槽位但不裁决。"""
        nonlocal single_cards
        for slot, card in enumerate(seg["cards"], start=1):
            single_cards += 1
            ref = {
                "track": track_no,
                "item": card["item"],
                "segment_index": slot - 1,
            }
            rescans.append(
                RescanEntry(
                    reason="single_side",
                    track=track_no,
                    item=card["item"],
                    segment=seg_index,
                    segment_label=seg["label"],
                    pending_needles=list(all_needles),
                    card_ref=ref,
                    detail="隔板不全或顺序冲突，该纹板未参与对齐，需复扫",
                )
            )
            master_entries.append(
                MasterEntry(
                    seq=seq,
                    segment=seg_index,
                    segment_label=seg["label"],
                    slot=slot,
                    kind="card",
                    mask="?" * nc,
                    source=f"track{track_no}",
                    card_ref=ref,
                    pending_needles=list(all_needles),
                )
            )

    # ---- 可安全对齐的段 ----
    for seg_index in range(alignable):
        s1, s2 = segs[0][seg_index], segs[1][seg_index]
        label = s1["label"]  # 相配隔板标号一致；头段为 None
        if seg_index > 0:
            add_separator_slot(matched_labels[seg_index - 1], seg_index)

        payload: dict[str, Any] = {
            "index": seg_index,
            "label": label,
            "aligned": True,
            "track1_cards": cards_payload(s1),
            "track2_cards": cards_payload(s2),
        }

        alignment: Alignment = align(
            [c["mask"] for c in s1["cards"]],
            [c["mask"] for c in s2["cards"]],
        )
        total_cost += alignment.cost
        total_gaps += alignment.gaps

        op_payloads: list[dict[str, Any]] = []
        slot = 0
        subst_cost = 0
        for op in alignment.ops:
            op_d: dict[str, Any] = {"op": op.op, "cost": op.cost}
            if op.op == "match":
                i, j = op.i, op.j
                c1, c2 = s1["cards"][i], s2["cards"][j]
                m1, m2 = c1["mask"], c2["mask"]
                slot += 1
                subst_cost += op.cost
                total_subst += op.cost
                pending = [k + 1 for k in range(nc) if m1[k] != m2[k]]
                consensus = "".join(
                    m1[k] if m1[k] == m2[k] else "?" for k in range(nc)
                )
                ref = {
                    "track1": {"item": c1["item"], "segment_index": i},
                    "track2": {"item": c2["item"], "segment_index": j},
                }
                if pending:
                    disagree_cards += 1
                    rescans.append(
                        RescanEntry(
                            reason="mask_disagree",
                            track=1,
                            item=c1["item"],
                            segment=seg_index,
                            segment_label=label,
                            other_track=2,
                            other_item=c2["item"],
                            pending_needles=pending,
                            card_ref=ref,
                            detail=(
                                f"两路纹板在 {len(pending)} 个针位上分歧"
                                f"（汉明距离 {op.cost}）"
                            ),
                        )
                    )
                add_card_slot(
                    seg_index, label, slot, "match", consensus, pending, ref
                )
                op_d["track1"] = {"item": c1["item"], "segment_index": i}
                op_d["track2"] = {"item": c2["item"], "segment_index": j}
                op_d["pending_needles"] = pending
            elif op.op == "delete":
                i = op.i
                c1 = s1["cards"][i]
                slot += 1
                single_cards += 1
                ref = {"track": 1, "item": c1["item"], "segment_index": i}
                rescans.append(
                    RescanEntry(
                        reason="single_side",
                        track=1,
                        item=c1["item"],
                        segment=seg_index,
                        segment_label=label,
                        pending_needles=list(all_needles),
                        card_ref=ref,
                        detail="仅第 1 路存在：第 2 路漏扫或本路回带重扫，需复扫裁决",
                    )
                )
                add_card_slot(
                    seg_index, label, slot, "track1", "?" * nc,
                    list(all_needles), ref,
                )
                op_d["track1"] = {"item": c1["item"], "segment_index": i}
            else:  # insert
                j = op.j
                c2 = s2["cards"][j]
                slot += 1
                single_cards += 1
                ref = {"track": 2, "item": c2["item"], "segment_index": j}
                rescans.append(
                    RescanEntry(
                        reason="single_side",
                        track=2,
                        item=c2["item"],
                        segment=seg_index,
                        segment_label=label,
                        pending_needles=list(all_needles),
                        card_ref=ref,
                        detail="仅第 2 路存在：第 1 路漏扫或本路回带重扫，需复扫裁决",
                    )
                )
                add_card_slot(
                    seg_index, label, slot, "track2", "?" * nc,
                    list(all_needles), ref,
                )
                op_d["track2"] = {"item": c2["item"], "segment_index": j}
            op_payloads.append(op_d)

        payload.update(
            {
                "cost": alignment.cost,
                "gaps": alignment.gaps,
                "substitution_cost": subst_cost,
                "match_count": len(alignment.pairs),
                "pairs": [[i, j] for i, j in alignment.pairs],
                "ops": op_payloads,
            }
        )
        segment_payloads.append(payload)

    # ---- 未决尾段与冲突点之后：两路各自剩余段，不再互相配对 ----
    if alignable < max(len(segs[0]), len(segs[1])):
        for track_no in (1, 2):
            for seg_index in range(alignable, len(segs[track_no - 1])):
                seg = segs[track_no - 1][seg_index]
                dump_unaligned(track_no, seg, seg_index)
                segment_payloads.append(
                    {
                        "index": seg_index,
                        "track": track_no,
                        "label": seg["label"],
                        "aligned": False,
                        f"track{track_no}_cards": cards_payload(seg),
                        "cost": None,
                        "gaps": None,
                        "pairs": [],
                        "ops": [],
                        "note": "隔板不全或顺序冲突，该段未对齐，全部纹板挂起待复扫",
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
            "matched_count": p,
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
                "aligned_segments": alignable,
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
