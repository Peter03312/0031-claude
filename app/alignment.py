"""单调全局对齐（Needleman-Wunsch 变体）。

代价规则：
- 替换（两张纹板配对）：掩码汉明距离；
- 插入 / 删除（单边纹板，漏扫或重扫）：固定代价 4。

裁决顺序：
1. 总代价最小；
2. 缺口（插入 + 删除）数量最少；
3. 配对索引序列字典序最小。

配对索引序列定义为命中配对的段内下标元组按对齐顺序排列：
``((i1, j1), (i2, j2), ...)``，按 Python 元组字典序比较。
该规则在代价、缺口数并列时，优先选择“尽早、尽量按下标自然顺序配对”的方案，
从源头避免在漏张处硬按下标合并造成的后续整链错位。
"""

from __future__ import annotations

from .models import Alignment, Op

GAP_COST = 4

# DP 单元的状态键：(总代价, 缺口数, 配对索引序列)
Key = tuple[int, int, tuple[tuple[int, int], ...]]


def hamming(a: str, b: str) -> int:
    """掩码汉明距离；调用方保证等长。"""
    return sum(x != y for x, y in zip(a, b))


def _better(candidate: Key, current: Key | None) -> bool:
    """候选状态是否严格优于当前状态。"""
    if current is None:
        return True
    # 元组逐分量比较：cost → gaps → pairs 字典序，正好对应三级裁决。
    return candidate < current


def align(a_cards: list[str], b_cards: list[str]) -> Alignment:
    m, n = len(a_cards), len(b_cards)

    # keys[r][c]：到达该格的最优状态；moves[r][c]：取得该状态的最后一步。
    keys: list[list[Key | None]] = [[None] * (n + 1) for _ in range(m + 1)]
    moves: list[list[str | None]] = [[None] * (n + 1) for _ in range(m + 1)]
    keys[0][0] = (0, 0, ())

    for r in range(m + 1):
        for c in range(n + 1):
            if r == 0 and c == 0:
                continue
            best: Key | None = None
            best_move: str | None = None

            # 删除：消耗 A[r-1]
            if r > 0 and keys[r - 1][c] is not None:
                pc, pg, pp = keys[r - 1][c]  # type: ignore[misc]
                cand = (pc + GAP_COST, pg + 1, pp)
                if _better(cand, best):
                    best, best_move = cand, "delete"

            # 插入：消耗 B[c-1]
            if c > 0 and keys[r][c - 1] is not None:
                pc, pg, pp = keys[r][c - 1]  # type: ignore[misc]
                cand = (pc + GAP_COST, pg + 1, pp)
                if _better(cand, best):
                    best, best_move = cand, "insert"

            # 配对：A[r-1] 与 B[c-1]（对角线，单调、不交叉）
            if r > 0 and c > 0 and keys[r - 1][c - 1] is not None:
                pc, pg, pp = keys[r - 1][c - 1]  # type: ignore[misc]
                sub = hamming(a_cards[r - 1], b_cards[c - 1])
                cand = (pc + sub, pg, pp + ((r - 1, c - 1),))
                # 对角线优先：并列时保留 match 而非 gap
                if best is None or cand < best:
                    best, best_move = cand, "match"

            keys[r][c] = best
            moves[r][c] = best_move

    final_key = keys[m][n]
    assert final_key is not None
    total_cost, total_gaps, pair_tuple = final_key

    # 回溯（移动选择已在填表时按三级裁决固化，结果唯一可复刻）
    ops_rev: list[Op] = []
    r, c = m, n
    while r > 0 or c > 0:
        move = moves[r][c]
        if move == "match":
            i, j = r - 1, c - 1
            ops_rev.append(Op("match", i, j, hamming(a_cards[i], b_cards[j])))
            r, c = i, j
        elif move == "delete":
            i = r - 1
            ops_rev.append(Op("delete", i, None, GAP_COST))
            r = i
        elif move == "insert":
            j = c - 1
            ops_rev.append(Op("insert", None, j, GAP_COST))
            c = j
        else:  # pragma: no cover - 不可能，防御性断言
            raise RuntimeError("alignment backtrace reached an empty cell")

    ops = list(reversed(ops_rev))
    return Alignment(
        ops=ops,
        cost=total_cost,
        gaps=total_gaps,
        pairs=[(i, j) for (i, j) in pair_tuple],
    )
