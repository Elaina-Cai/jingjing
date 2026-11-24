"""反应清单数据与渲染单测（只读，不影响行为）。

Run directly:
    python tests/test_react_playbook.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.speech.react_playbook import (  # noqa: E402
    BLOCK_HEADER,
    PlaybookScene,
    ReactPlaybook,
    load_playbook,
    position_map,
    render_playbook_block,
    render_scene,
)


def test_seed_schema():
    book = load_playbook()
    assert book.version, "version 不能为空"
    assert len(book.usage) == 3, book.usage
    assert len(book.common) == 6, book.common
    assert len(book.scenes) == 19, len(book.scenes)
    ids = [scene.id for scene in book.scenes]
    assert len(ids) == len(set(ids)), ids
    for scene in book.scenes:
        assert scene.name and scene.signals and scene.options and scene.forbidden, scene.id
        assert scene.enabled is True, scene.id
    upset = book.scene("user_upset")
    assert upset is not None
    assert any("只丢一句" in item for item in upset.forbidden), upset.forbidden
    assert any("具体的点" in item for item in upset.options), upset.options
    assert "谁" not in " ".join(upset.options), upset.options
    assert any("追问归因" in item for item in upset.forbidden), upset.forbidden
    assert all("谁惹你了" not in item for item in upset.forbidden), upset.forbidden
    return True


def test_render_all_scenes():
    block = render_playbook_block()
    assert block.startswith(BLOCK_HEADER), block[:40]
    assert "用法：" in block and "只照那一幕的选项来" in block
    assert "通用：" in block
    for scene in load_playbook().scenes:
        assert f"【{scene.name}】" in block, scene.id
        for option in scene.options:
            assert option in block, (scene.id, option)
        for forbidden in scene.forbidden:
            assert forbidden in block, (scene.id, forbidden)
    return True


def test_scene_filter_is_deterministic():
    block = render_playbook_block(scene_ids=["patted_teased"])
    assert "被拍一拍" in block
    assert "被嫌话多" not in block
    other = render_playbook_block(scene_ids=["said_too_talkative"])
    assert "被嫌话多" in other
    assert "被拍一拍" not in other
    return True


def test_positions_schema():
    """WP-2A：8 条「位置 → 默认姿态」映射已迁入数据（唯一数据源）。"""
    from core.context.stance import STANCES

    book = load_playbook()
    assert len(book.positions) == 8, len(book.positions)
    for item in book.positions:
        assert item.position and item.stance and item.reason, item
        assert item.stance in STANCES, (item.position, item.stance)
    mapped = position_map()
    assert len(mapped) == 8, mapped
    assert mapped["被指使/被让传话"][0] == "拆台"
    assert "先落库再回话" in mapped["被指使/被让传话"][1]
    return True


def test_audience_filter():
    """WP-2A：按生效场景过滤；空 audience = 不过滤（等价判据）。"""
    book = load_playbook()
    private = render_playbook_block(book, audience="private")
    group = render_playbook_block(book, audience="group")
    for scene in book.scenes:
        token = f"【{scene.name}】"
        if "private" in scene.scenes:
            assert token in private, scene.id
        else:
            assert token not in private, scene.id
        if "group" in scene.scenes:
            assert token in group, scene.id
        else:
            assert token not in group, scene.id
    bg = book.scene("background_group")
    assert bg is not None
    assert f"【{bg.name}】" not in private
    assert f"【{bg.name}】" in group
    opener = book.scene("proactive_opener")
    assert opener is not None and opener.scenes == ("private",)
    assert "【主动开口】" in private and "【主动开口】" not in group
    assert render_playbook_block(book, audience="") == render_playbook_block(book)
    return True


def test_disabled_scene_skipped():
    book = ReactPlaybook(
        version="test",
        common=("先接住眼前这句。",),
        scenes=(
            PlaybookScene(id="a", name="场景A", options=("轻接",), forbidden=("甩回",), enabled=True),
            PlaybookScene(id="b", name="场景B", options=("收声",), forbidden=("赌气",), enabled=False),
        ),
    )
    block = render_playbook_block(book)
    assert "场景A" in block and "场景B" not in block
    assert render_scene(book.scenes[0]).startswith("【场景A】")
    return True


def test_missing_or_broken_file_is_empty():
    assert load_playbook("Z:/definitely/not/here.json").scenes == ()
    assert render_playbook_block(load_playbook("Z:/definitely/not/here.json")) == ""
    return True


def main() -> bool:
    tests = [
        test_seed_schema,
        test_render_all_scenes,
        test_scene_filter_is_deterministic,
        test_positions_schema,
        test_audience_filter,
        test_disabled_scene_skipped,
        test_missing_or_broken_file_is_empty,
    ]
    print("=" * 50)
    print("反应清单数据/渲染测试")
    print("=" * 50)
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"\nreact-playbook checks: {len(tests) - len(failed)}/{len(tests)} passed")
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
