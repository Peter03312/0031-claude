"""域模型：请求、对齐操作、合片结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ItemType = Literal["card", "separator"]


@dataclass(frozen=True)
class Item:
    type: ItemType
    # card: 孔位掩码，'0'/'1' 串；separator: None
    mask: str | None = None
    # separator: 唯一标号；card: None
    label: str | None = None


@dataclass(frozen=True)
class Track:
    items: list[Item]
    name: str | None = None

    @property
    def cards(self) -> list[Item]:
        return [it for it in self.items if it.type == "card"]

    @property
    def separators(self) -> list[Item]:
        return [it for it in self.items if it.type == "separator"]


@dataclass(frozen=True)
class MergeRequest:
    needle_count: int
    tracks: tuple[Track, Track]
    name: str | None = None


# ---------- 对齐 ----------

# op 的 i/j 为段内纹板 0 基下标；gap 侧下标为 None
OpKind = Literal["match", "insert", "delete"]


@dataclass(frozen=True)
class Op:
    op: OpKind
    i: int | None  # 第一路段内下标
    j: int | None  # 第二路段内下标
    cost: int


@dataclass
class Alignment:
    ops: list[Op]
    cost: int
    gaps: int
    # 对齐命中的 (i, j) 有序元组，即“配对索引序列”
    pairs: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class SegmentResult:
    index: int
    label: str | None  # None 表示头段（第一张隔板之前）
    a_cards: list[str]
    b_cards: list[str]
    alignment: Alignment


@dataclass
class MasterEntry:
    """可复刻母版中的一个槽位。"""

    seq: int  # 母版全局序号，从 1 开始
    segment: int  # 段号，从 0 开始
    segment_label: str | None
    slot: int  # 段内槽位序号，从 1 开始
    kind: Literal["card", "separator"]
    # card 时：'0'/'1' 表示确定孔位，'?' 表示未决针位
    mask: str | None = None
    # 本槽位由哪一路提供 / 双方匹配
    source: str | None = None  # "match" | "track1" | "track2" | None
    card_ref: dict[str, Any] = field(default_factory=dict)
    pending_needles: list[int] = field(default_factory=list)
    label: str | None = None


@dataclass
class RescanEntry:
    """复扫清单条目。"""

    reason: Literal[
        "single_side",        # 单边纹板（漏扫 / 重扫由对齐上下文体现）
        "mask_disagree",      # 配对纹板针位分歧
        "separator_missing",  # 另一路缺少隔板
        "separator_order",    # 两路隔板同序校验失败
    ]
    track: int | None
    item: int | None  # 该路原序列中的 1 基位置
    segment: int | None
    segment_label: str | None
    label: str | None = None
    other_track: int | None = None
    other_item: int | None = None
    pending_needles: list[int] = field(default_factory=list)
    card_ref: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
