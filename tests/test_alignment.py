"""对齐算法单元测试：汉明代价、缺口计数、三级裁决。"""

from __future__ import annotations

import random

from app.alignment import GAP_COST, align, hamming


def _replay_cost(masks_a: list[str], masks_b: list[str], ops) -> int:
    total = 0
    for op in ops:
        if op.op == "match":
            total += hamming(masks_a[op.i], masks_b[op.j])
        else:
            total += GAP_COST
    return total


def test_hamming_basic() -> None:
    assert hamming("1010", "1010") == 0
    assert hamming("1010", "1110") == 1
    assert hamming("0000", "1111") == 4


def test_empty_tracks() -> None:
    r = align([], [])
    assert r.cost == 0
    assert r.gaps == 0
    assert r.pairs == []
    assert r.ops == []


def test_one_side_empty_is_all_gaps() -> None:
    a = ["1010", "0101"]
    r = align(a, [])
    assert r.cost == 2 * GAP_COST
    assert r.gaps == 2
    assert [op.op for op in r.ops] == ["delete", "delete"]
    assert _replay_cost(a, [], r.ops) == r.cost

    r2 = align([], a)
    assert r2.cost == 2 * GAP_COST
    assert [op.op for op in r2.ops] == ["insert", "insert"]


def test_missing_card_does_not_shift_later_match() -> None:
    # 第二路在中间漏扫一张：第三张绝不能被硬并到第二张上
    a = ["11110000", "10101010", "00001111"]
    b = ["11110000", "00001111"]
    r = align(a, b)
    assert r.pairs == [(0, 0), (2, 1)]
    assert r.gaps == 1
    assert r.cost == GAP_COST
    assert [op.op for op in r.ops] == ["match", "delete", "match"]
    assert _replay_cost(a, b, r.ops) == r.cost


def test_duplicate_scan_is_gap_not_forced_match() -> None:
    # 第二路回带重扫了第一张：多出的副本按缺口处理，后续不错位
    a = ["11110000", "00001111"]
    b = ["11110000", "11110000", "00001111"]
    r = align(a, b)
    assert r.pairs == [(0, 0), (1, 2)]
    assert r.gaps == 1
    assert r.cost == GAP_COST
    assert [op.op for op in r.ops] == ["match", "insert", "match"]


def test_dust_disagreement_is_cheap_substitution() -> None:
    # 一路把灰尘识别成孔：汉明距离 1（< 缺口代价 8），配对成功但针位挂起
    a = ["10101010"]
    b = ["10111010"]
    r = align(a, b)
    assert r.pairs == [(0, 0)]
    assert r.gaps == 0
    assert r.cost == 1
    assert r.ops[0].cost == 1


def test_full_mismatch_cost_boundary() -> None:
    # 汉明距离 = 针位数 4：单张配对代价 4 < 双缺口 8，仍配对（裁决分歧针位）
    a = ["1111"]
    b = ["0000"]
    r = align(a, b)
    assert r.cost == 4
    assert r.gaps == 0
    assert r.pairs == [(0, 0)]


def test_tie_break_fewest_gaps() -> None:
    # 方案「配 0,0(距离1) + 缺口」= 5/1缺口
    # 方案「缺口 + 配 1,0(距离0)」= 4/1缺口 → 总代价优先选后者
    a = ["11110000", "00000000"]
    b = ["00000000"]
    r = align(a, b)
    assert r.pairs == [(1, 0)]
    assert r.cost == GAP_COST
    assert r.gaps == 1

    # 代价并列、缺口数不同：有缺口路径必须让位于零缺口路径
    # A=[1000,0001], B=[0001,1000]：单调同向配对每张汉明距离 2，共 4、0 缺口；
    # 反向的零汉明配对 (0,1),(1,0) 交叉，单调对齐不允许
    r2 = align(["1000", "0001"], ["0001", "1000"])
    assert r2.pairs == [(0, 0), (1, 1)]
    assert r2.gaps == 0
    assert r2.cost == 4


def test_tie_break_lexicographic_pairs_explicit() -> None:
    # 真实字典序平局：a=000, b=111(与 a 距离3), c=100(与 b 距离2)
    # 方案 P：配 (a,b) 距离3 + 删 c 4 = 7，1缺口，序列 ((0,0),)
    # 方案 Q：删 a 4 + 配 (b,c) 距离2 = 6，1缺口 → 总代价更小，非平局
    # 调整 c=110，h(b,c)=1：P=3+4=7，Q=4+1=5，仍非平局。
    # 令 c 使 h(b,c)=3（c=000 即与 a 相同）：
    #   P: 配(a,b)=3 + 删 c=4 = 7；Q: 删 a=4 + 配(c,b)=3 = 7
    #   两者代价 7、缺口 1 完全相同；配对序列 ((0,0),) < ((1,0),)
    a, b, c = "000", "111", "000"
    assert hamming(a, b) == 3 and hamming(c, b) == 3
    r = align([a, c], [b])
    assert r.cost == 3 + GAP_COST
    assert r.gaps == 1
    assert r.pairs == [(0, 0)]  # 字典序最小者，而非 ((1, 0),)
    kinds = [op.op for op in r.ops]
    assert kinds == ["match", "delete"]


def test_bruteforce_cross_check() -> None:
    """随机小例穷举所有单调对齐，独立验证三级裁决。"""
    rng = random.Random(20260913)
    for _ in range(80):
        n, m = rng.randint(0, 4), rng.randint(0, 4)
        cards_a = ["".join(rng.choice("01") for _ in range(3)) for _ in range(n)]
        cards_b = ["".join(rng.choice("01") for _ in range(3)) for _ in range(m)]
        r = align(cards_a, cards_b)
        assert _replay_cost(cards_a, cards_b, r.ops) == r.cost
        assert _brute_best(cards_a, cards_b) == (
            r.cost,
            r.gaps,
            tuple(r.pairs),
        )


def _brute_best(
    a: list[str], b: list[str]
) -> tuple[int, int, tuple[tuple[int, int], ...]]:
    best: tuple[int, int, tuple[tuple[int, int], ...]] | None = None

    def rec(i: int, j: int, cost: int, gaps: int,
            pairs: tuple[tuple[int, int], ...]) -> None:
        nonlocal best
        if i == len(a) and j == len(b):
            key = (cost, gaps, pairs)
            if best is None or key < best:
                best = key
            return
        if i < len(a) and j < len(b):
            rec(i + 1, j + 1, cost + hamming(a[i], b[j]), gaps,
                pairs + ((i, j),))
        if i < len(a):
            rec(i + 1, j, cost + GAP_COST, gaps + 1, pairs)
        if j < len(b):
            rec(i, j + 1, cost + GAP_COST, gaps + 1, pairs)

    rec(0, 0, 0, 0, ())
    assert best is not None
    return best


def test_monotonicity_and_index_coverage() -> None:
    a = ["100", "010", "001", "110"]
    b = ["100", "001", "111"]
    r = align(a, b)
    ii = [p[0] for p in r.pairs]
    jj = [p[1] for p in r.pairs]
    assert ii == sorted(ii) and len(set(ii)) == len(ii)
    assert jj == sorted(jj) and len(set(jj)) == len(jj)
    used_i = {op.i for op in r.ops if op.op != "insert"}
    used_j = {op.j for op in r.ops if op.op != "delete"}
    assert used_i == set(range(len(a)))
    assert used_j == set(range(len(b)))


def test_determinism() -> None:
    a = ["1010", "0101", "1111"]
    b = ["1010", "1111", "0000"]
    r1 = align(a, b)
    r2 = align(a, b)
    assert (r1.cost, r1.gaps, r1.pairs) == (r2.cost, r2.gaps, r2.pairs)
