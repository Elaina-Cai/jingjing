"""配方加载器：写错了必须报错，不能静默略过（U-20）。

跑法：``python -X utf8 -u tests/test_app_recipes.py``
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui import recipe as R  # noqa: E402

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name} {detail}")


GOOD = """
name: demo
version: 1
enabled: true
window_title: 记事本
effects: [read, ui_control]
steps:
  - n: 1
    do: wake
    expect: 前台是记事本
  - n: 2
    do: click
    target: "#editor"
    expect: 光标进了编辑区
  - n: 3
    do: type
    target: "#editor"
    text: 你好
    expect: 编辑区里出现「你好」
"""


def _parse(text: str) -> R.Recipe:
    import yaml
    return R.parse_recipe(yaml.safe_load(text))


def _raises(fn) -> str:
    try:
        fn()
    except R.RecipeError as exc:
        return exc.code
    return ""


def test_good() -> None:
    r = _parse(GOOD)
    check("好配方：名字", r.name == "demo", r.name)
    check("好配方：三步", len(r.steps) == 3, str(len(r.steps)))
    check("好配方：步号连续", [s.n for s in r.steps] == [1, 2, 3], str([s.n for s in r.steps]))
    check("好配方：expect 必填有值", all(s.expect for s in r.steps))
    check("好配方：click 的 target 保留", r.steps[1].target == "#editor", r.steps[1].target)
    check("好配方：type 的正文保留", r.steps[2].text == "你好", r.steps[2].text)
    check("好配方：有会动的步骤", r.needs_motion is True)
    check(
        "好配方：effects 是声明的超集",
        set(r.effects) == {"read", "ui_control"},
        str(r.effects),
    )
    check("好配方：能按步号取", r.step(2) is not None and r.step(2).do == "click")
    check("好配方：取不到返回 None", r.step(9) is None)
    check("描述里不含正文", "你好" not in R.describe(r), R.describe(r))
    check("描述里有步数与窗口", "3 步" in R.describe(r) and "记事本" in R.describe(r))
    check("as_dict 可序列化", isinstance(r.as_dict()["steps"], list))


def test_derived_effects() -> None:
    text = """
name: readonly
window_title: Edge
steps:
  - n: 1
    do: wake
    expect: 前台是 Edge
"""
    r = _parse(text)
    check("不写 effects 时按步骤推出 read", r.effects == ("read",), str(r.effects))

    text2 = text.replace(
        "window_title: Edge",
        "window_title: Edge\neffects: [read]",
    ).replace(
        "expect: 前台是 Edge",
        "expect: 前台是 Edge\n  - n: 2\n    do: click\n    target: '#a'\n    expect: 点到了",
    )
    check("声明了 read 却有 click → effects_insufficient", _raises(lambda: _parse(text2)) == "effects_insufficient")

    text3 = text2.replace("effects: [read]", "effects: [read, ui_control]")
    r3 = _parse(text3)
    check("声明齐了就能过", set(r3.effects) == {"read", "ui_control"}, str(r3.effects))


def test_fail_closed() -> None:
    cases = [
        ("没有 expect", GOOD.replace("    expect: 前台是记事本\n", ""), "empty_field"),
        ("expect 是空白", GOOD.replace("expect: 前台是记事本", "expect: '   '"), "empty_field"),
        ("do 不认识", GOOD.replace("do: wake", "do: jump"), "bad_do"),
        ("未知字段", GOOD.replace("    expect: 前台是记事本", "    expect: 前台是记事本\n    expec: 拼错"), "unknown_field"),
        ("步号跳号", GOOD.replace("  - n: 2", "  - n: 5"), "bad_step_number"),
        ("步号重号", GOOD.replace("  - n: 3", "  - n: 2"), "bad_step_number"),
        ("click 没 target", GOOD.replace("    target: \"#editor\"\n    expect: 光标进了编辑区", "    expect: 光标进了编辑区"), "empty_target"),
        ("navigate 不是 http", GOOD.replace("do: click", "do: navigate").replace('target: "#editor"', "target: www.example.com"), "bad_url"),
        ("type 带控制字符", GOOD.replace("text: 你好", 'text: "a\\tb"'), "bad_typed_text"),
        ("wait 超界", """
name: w
steps:
  - n: 1
    do: wait
    wait_s: 31
    expect: 等
""", "bad_wait"),
        ("wait 为 0", """
name: w
steps:
  - n: 1
    do: wait
    wait_s: 0
    expect: 等
""", "bad_wait"),
        ("配方里写 external_send", GOOD.replace("effects: [read, ui_control]", "effects: [read, ui_control, external_send]"), "forbidden_effect"),
        ("有 click 却没窗口标识", GOOD.replace("window_title: 记事本\n", ""), "no_window"),
        ("name 有中文", GOOD.replace("name: demo", "name: 演示"), "bad_name"),
        ("enabled 不是布尔", GOOD.replace("enabled: true", "enabled: 'yes'"), "bad_enabled"),
        ("version 是 0", GOOD.replace("version: 1", "version: 0"), "bad_version"),
        ("顶层不是映射", "- a\n- b\n", "bad_shape"),
        ("steps 是空的", "name: x\nsteps: []\n", "no_steps"),
    ]
    for label, text, want in cases:
        got = _raises(lambda t=text: _parse(t))
        check(f"拒绝：{label} → {want}", got == want, f"got={got!r}")

    too_many = "name: big\nsteps:\n" + "".join(
        f"  - n: {i}\n    do: wait\n    wait_s: 1\n    expect: 等\n" for i in range(1, 14)
    )
    check("拒绝：超过 12 步", _raises(lambda: _parse(too_many)) == "too_many_steps")


def test_files() -> None:
    with tempfile.TemporaryDirectory(dir=str(ROOT / ".test_tmp")) as tmp:
        d = Path(tmp)
        (d / "a.yaml").write_text(GOOD, encoding="utf-8")
        (d / "b.yaml").write_text(GOOD.replace("name: demo", "name: demo2").replace("enabled: true", "enabled: false"), encoding="utf-8")
        (d / "_template.yaml").write_text(GOOD.replace("name: demo", "name: tpl"), encoding="utf-8")
        loaded = R.load_recipes(d)
        check("只加载非下划线文件", set(loaded) == {"demo", "demo2"}, str(sorted(loaded)))
        check("enabled_recipes 只给启用的", [r.name for r in R.enabled_recipes(d)] == ["demo"], str([r.name for r in R.enabled_recipes(d)]))
        check("按名字排序", list(loaded) == ["demo", "demo2"], str(list(loaded)))

        (d / "bad.yaml").write_text("name: bad\nsteps: []\n", encoding="utf-8")
        check("坏配方不会被静默略过", _raises(lambda: R.load_recipes(d)) == "no_steps")

        (d / "bad.yaml").unlink()
        (d / "c.yaml").write_text(GOOD, encoding="utf-8")
        check("同名配方报错", _raises(lambda: R.load_recipes(d)) == "duplicate_name")

    check("目录不存在时是空表", R.load_recipes(ROOT / ".test_tmp" / "no_such_dir") == {})
    check("默认目录存在", (ROOT / R.APP_DIR).is_dir())
    check("模板文件不被加载", R.load_recipes(ROOT / R.APP_DIR) == {}, str(R.load_recipes(ROOT / R.APP_DIR)))
    check("模板本身写得合法（去掉下划线就能加载）", R.load_recipe_file(ROOT / R.APP_DIR / "_example.yaml").name == "demo_reader")


def test_discipline() -> None:
    src = (ROOT / "core/execution/gui/recipe.py").read_text(encoding="utf-8")
    for banned in ("core.speech", "core.routing", "agents", "scheduler", "web."):
        check(f"配方层不 import {banned}", f"import {banned}" not in src and f"from {banned}" not in src)
    for banned in ("dispatchMouseEvent", "SendInput", "press_enter", "subprocess"):
        check(f"配方层不含 {banned}", banned not in src)
    check("external_send 只作为禁用词出现", src.count("external_send") == 2, str(src.count("external_send")))
    check("MAX_STEPS 是 12", R.MAX_STEPS == 12)


def main() -> int:
    test_good()
    test_derived_effects()
    test_fail_closed()
    test_files()
    test_discipline()
    print(f"RESULT {FAILED} failed")
    return FAILED


if __name__ == "__main__":
    raise SystemExit(main())
