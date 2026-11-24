"""短句种子 schema 校验（只读，不接运行时）。

Run directly:
    python tests/test_short_reply_seed.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = ROOT / "configs" / "speech_seed" / "short_reply_seed.json"

EXPECTED_STATES = {"平常", "别扭", "低电量", "冷", "正事", "特殊"}
EXPECTED_SCENES = {
    "平常": {
        "叫名字", "在吗", "被夸", "被谢", "求她办事", "被麻烦", "显摆", "被小看",
        "收到/被交代", "被关心", "对方难过", "道别", "晚安", "抬杠", "被惊到",
        "追问", "突然想起",
    },
    "别扭": {"被戳穿_你在等我吧", "被戳穿_你是不是在意", "被说_你嘴硬", "她道歉", "被道歉"},
    "低电量": {"被叫", "被问在吗"},
    "冷": {"被冒犯", "继续逗"},
    "正事": {"查到了", "记提醒", "拒接传话", "办成了"},
    "特殊": {"打错字_对方打错", "打错字_自己手滑", "被表白", "搞砸", "被抓包"},
}
DASH_RE = re.compile(r"[-\u2010-\u2015\u2212\uFF0D]")
ELLIPSIS_RE = re.compile(r"…+")


def _load() -> dict:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


def _count_chars(text: str) -> int:
    """与主人原表口径一致：中文逗号不计；'……' 算一个字；其余标点照算。"""
    compact = text.replace("……", "…").replace("，", "").replace(",", "")
    return len(compact)


def test_seed_schema() -> bool:
    data = _load()
    assert data["key"] == "yinyue_short_reply_seed"
    assert data["version"]
    assert data["name"]

    rules = data["rules"]
    assert rules["count_policy"] == "ignore_cjk_comma_and_count_ellipsis_as_one"
    assert rules["default_min_chars"] == 2
    assert rules["default_max_chars"] == 8
    assert rules["serious_max_chars"] >= rules["default_max_chars"]
    assert rules["ellipsis_max_per_reply"] == 1
    assert rules["forbid_dash"] is True
    assert rules["forbidden_chars"] == ["哦"]

    single_allowed = set(rules["single_char_replies"])
    assert single_allowed, "single_char_replies 不能为空"
    assert len(single_allowed) == len(rules["single_char_replies"]), "单字白名单不能重复"

    banned_in_states = rules["forbidden_in_states"]
    assert "别扭" in banned_in_states and "嘿嘿" in banned_in_states["别扭"]
    assert "冷" in banned_in_states and "嘿嘿" in banned_in_states["冷"]

    states = data["states"]
    assert set(states) == EXPECTED_STATES, set(states)

    total_lines = 0
    all_lines: list[str] = []
    for state, scenes in states.items():
        assert isinstance(scenes, dict) and scenes, state
        missing = EXPECTED_SCENES[state] - set(scenes)
        assert not missing, (state, missing)
        for scene, lines in scenes.items():
            assert isinstance(scene, str) and scene.strip() == scene, (state, scene)
            assert isinstance(lines, list) and lines, (state, scene)
            assert len(lines) == len(set(lines)), (state, scene, "同场景内不能重复")
            for text in lines:
                assert isinstance(text, str), (state, scene, text)
                assert text == text.strip(), (state, scene, text)
                assert "\n" not in text and "\r" not in text, (state, scene, text)
                assert not DASH_RE.search(text), (state, scene, text, "不允许破折号")
                assert len(ELLIPSIS_RE.findall(text)) <= rules["ellipsis_max_per_reply"], (state, scene, text, "省略号最多一个")
                for forbidden in rules["forbidden_chars"]:
                    assert forbidden not in text, (state, scene, text, forbidden)
                for forbidden in banned_in_states.get(state, ()):
                    assert forbidden not in text, (state, scene, text, forbidden)

                length = _count_chars(text)
                assert length >= 1, (state, scene, text)
                if length == 1:
                    assert text in single_allowed, (state, scene, text, "单字不在白名单")
                else:
                    assert length >= rules["default_min_chars"], (state, scene, text, length)

                limit = rules["serious_max_chars"] if state == "正事" else rules["default_max_chars"]
                assert length <= limit, (state, scene, text, length, limit)
                if length > rules["default_max_chars"]:
                    assert state == "正事", (state, scene, text, length, "只有正事允许超过 8 字")

                total_lines += 1
                all_lines.append(text)

    assert total_lines >= 100, f"短句总数异常少: {total_lines}"
    assert len(set(all_lines)) >= 80, f"去重后短句数量异常少: {len(set(all_lines))}"

    # 已确认删除项回归
    for deleted in ("就这", "吓我一跳", "我还以为啥事呢"):
        assert deleted not in all_lines, deleted
    assert states["特殊"]["打错字_对方打错"] == ["噢", "噢噢"]
    assert "嘿嘿，没有" in states["平常"]["被夸"]
    assert all("嘿嘿，没有" not in lines for lines in states["别扭"].values())
    assert all("哦" not in line for line in all_lines)
    return True


def main() -> bool:
    try:
        test_seed_schema()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL test_seed_schema: {exc!r}")
        return False
    print("ok   test_seed_schema")
    print("\nshort-reply seed checks: 1/1 passed")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
