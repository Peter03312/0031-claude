"""畸形输入 fuzz 回归：任何怪值都只能产生可操作的 RequestError（400），
绝不允许 UnicodeEncodeError / TypeError / KeyError 等逃出（对应 500）。"""

from __future__ import annotations

import random

import pytest

from app.errors import RequestError
from app.merge import result_for_body

from tests.test_merge import body, sep


def _rnd(rng: random.Random) -> object:
    k = rng.randrange(8)
    if k == 0:
        return None
    if k == 1:
        return rng.choice([True, False])
    if k == 2:
        return rng.randrange(-2, 12)
    if k == 3:
        return "".join(
            rng.choice("01xS \t分隔板") for _ in range(rng.randrange(0, 10))
        )
    if k in (4, 5):
        return [_rnd(rng) for _ in range(rng.randrange(0, 4))]
    return {
        rng.choice([
            "type", "mask", "label", "items", "name",
            "needle_count", "tracks", "x",
        ]): _rnd(rng)
        for _ in range(rng.randrange(0, 4))
    }


@pytest.mark.parametrize("seed", range(120))
def test_random_malformed_input_never_crashes(seed: int) -> None:
    rng = random.Random(seed)
    try:
        result_for_body(_rnd(rng))  # 结构恰好合法时正常产出结果
    except RequestError:
        pass  # 预期：可定位、可操作的输入错误


def test_lone_surrogate_in_every_text_field() -> None:
    bad = "\ud800"
    cases = [
        {"needle_count": 4, "name": bad,
         "tracks": [{"items": []}, {"items": []}]},
        {"needle_count": 4, "tracks": [
            {"name": bad, "items": []}, {"items": []}]},
        {"needle_count": 4, "tracks": [
            {"items": [{"type": "separator", "label": bad}]}, {"items": []}]},
    ]
    for payload in cases:
        with pytest.raises(RequestError) as ei:
            result_for_body(payload)
        assert ei.value.code.endswith("_invalid") or ei.value.code.endswith(
            "_too_long"
        )


def test_extremely_long_label_rejected() -> None:
    with pytest.raises(RequestError) as ei:
        result_for_body(
            body(
                [sep("S" * 10_000)],
                [sep("S" * 10_000)],
            )
        )
    assert ei.value.code == "separator_label_too_long"
