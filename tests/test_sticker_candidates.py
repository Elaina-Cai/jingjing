# -*- coding: utf-8 -*-
"""P0-1 表情包候选语义（穷举整改，2026-10-16）：写时不承诺、读时透镜。

覆盖四件事：
  * ``sticker_store.looks_encoded`` / ``Sticker.description`` —— 脏 label 只在一处被
    识别，且是**读时投影**（registry.json 一个字节都不改）；
  * ``sticker_store.match_candidates`` / ``StickerStore.candidates`` —— 一个别名对上
    多条时**全部返回**（含 siblings），精确名短路；不再静默取第一条；
  * ``agents.tools.send_sticker`` —— ``YINYUE_FEATURE_STICKER_CANDIDATES`` 三态、
    schema 的 enum 只在 off/shadow 出现、歧义时弃权而不是挑一个最像的；
  * ``core.execution.wx_sticker_sender._hunt_outcome`` —— 「没搜完」与「不存在」分形。

Run directly:  python tests/test_sticker_candidates.py
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import sticker_store  # noqa: E402
from agents.tools import send_sticker as tool_mod  # noqa: E402
from core.execution import wx_sticker_sender as sender_mod  # noqa: E402

ENV = "YINYUE_FEATURE_STICKER_CANDIDATES"
ACCOUNT = "wxid_seed02"
DIRTY_LABEL = "Cg4KB2RlZmF1bHQSA+WvuQoOCgdkZWZhdWx0EgPlr7k="


def _sticker(emotion: str, **kw) -> "sticker_store.Sticker":
    return sticker_store.Sticker(emotion, "wechat", ACCOUNT, **kw)


def _set_mode(value) -> None:
    if value is None:
        os.environ.pop(ENV, None)
    else:
        os.environ[ENV] = value


class _entries_swapped:
    """Monkeypatch `sendable_stickers` so the tool tests do not depend on disk."""

    def __init__(self, entries):
        self.entries = entries

    def __enter__(self):
        self.original = sticker_store.sendable_stickers
        sticker_store.sendable_stickers = lambda platform=None, account_id=None: list(self.entries)
        return self.entries

    def __exit__(self, *exc):
        sticker_store.sendable_stickers = self.original
        return False


def test_looks_encoded() -> bool:
    """机器噪声 label 判得出来；中文短词 / slug / md5 一个都不误判。"""
    cases = (
        (DIRTY_LABEL, True),
        ("Cg4KB2RlZmF1bHQSA+WvuQoOCgdkZWZhdWx0EgPlr7k=", True),
        ("想显得呆呆的时使用", False),
        ("日常2", False),
        ("3753d84784f2da4215903aae61dc4777", False),   # md5 是 id，不是噪声 label
        ("my_sticker", False),
        ("", False),
        ("对", False),
    )
    ok = True
    for text, expect in cases:
        got = sticker_store.looks_encoded(text)
        if got != expect:
            ok = False
            print(f"  x looks_encoded({text[:24]!r}) = {got}（期望 {expect}）")
    print(f"  {'OK' if ok else 'x'} 机器噪声 label 判定: {len(cases)} 例")
    return ok


def test_description_lens() -> bool:
    """description = context → label → emotion；脏值跳过，不改数据。"""
    cases = (
        (_sticker("喝酒", context="想装醉时", label="日常"), "想装醉时"),
        (_sticker("点头", context="", label="对"), "对"),
        (_sticker("点头", context=DIRTY_LABEL, label=DIRTY_LABEL), "点头"),
        (_sticker("token拿来", context="", label=DIRTY_LABEL), "token拿来"),
    )
    ok = True
    for sticker, expect in cases:
        got = sticker.description
        if got != expect:
            ok = False
            print(f"  x {sticker.emotion!r}: {got!r}（期望 {expect!r}）")
    print(f"  {'OK' if ok else 'x'} 读时投影 description: {len(cases)} 例")
    return ok


def test_match_candidates_one_to_many() -> bool:
    """别名一对多必须全给；精确名短路；空/未命中给空元组。"""
    exact = _sticker("你好", label="日常")
    a = _sticker("点头", label="对", aliases=("对的", "是的", "你好"))
    b = _sticker("敬礼，遵命", label="对", aliases=("对的",))
    entries = [exact, a, b]
    ok = True

    got = sticker_store.match_candidates("你好", entries)
    if [c.emotion for c in got] != ["你好"] or got[0].matched_by != "name":
        ok = False
        print(f"  x 精确名短路: {[(c.emotion, c.matched_by) for c in got]}")

    got = sticker_store.match_candidates("对的", entries)
    if sorted(c.emotion for c in got) != sorted(["点头", "敬礼，遵命"]):
        ok = False
        print(f"  x 别名一对多: {[c.emotion for c in got]}")
    elif [c.matched_by for c in got] != ["alias", "alias"]:
        ok = False
        print(f"  x 别名命中理由: {[c.matched_by for c in got]}")
    elif any(len(c.siblings) != 1 for c in got):
        ok = False
        print(f"  x siblings 应互指: {[c.siblings for c in got]}")

    if sticker_store.match_candidates("", entries) != ():
        ok = False
        print("  x 空 key 不应命中")
    if sticker_store.match_candidates("没登记过的说法", entries) != ():
        ok = False
        print("  x 未命中应返回空元组（不许猜）")

    row = sticker_store.match_candidates("对的", entries)[0].as_dict()
    for key in ("emotion", "matched_by", "field", "value", "siblings", "description", "scope"):
        if key not in row:
            ok = False
            print(f"  x as_dict 缺字段 {key}")
    print(f"  {'OK' if ok else 'x'} match_candidates 一对多/短路/未命中")
    return ok


def test_candidates_shape() -> bool:
    """StickerCandidate 的两个字段名（matched_by / matched_field）不许改口径。"""
    a = _sticker("点头", label="对", aliases=("对的",))
    got = sticker_store.match_candidates("对的", [a])
    ok = got and got[0].matched_by == "alias" and got[0].matched_field == "aliases"
    if not ok:
        print(f"  x candidate = {got[0] if got else None}")
    print(f"  {'OK' if ok else 'x'} candidate 命中来源字段: {got[0].matched_field if got else '-'}")
    return bool(ok)


def test_live_registry_alias_completeness() -> bool:
    """真实 registry：声明了同一别名的条目必须**全部**出现在候选里。

    唯一例外是精确名短路——某个别名恰好等于另一条的名字时，名字是更强的证据
    （``resolve()`` 的老契约），那条被遮蔽的声明不会被静默丢掉，而是记在这里的
    shadowed 计数里，供数据侧处理（谁声明谁负责，代码不替数据删）。
    """
    store = sticker_store.StickerStore("wechat", ACCOUNT)
    if not store.exists():
        print("  SKIP 真实 registry 不在（隔离环境）")
        return True
    stickers = list(store.load().values())
    names = {s.emotion for s in stickers}
    checked = 0
    shadowed = []
    ok = True
    for sticker in stickers:
        for alias in sticker.aliases or ():
            got = [c.emotion for c in store.candidates(alias)]
            checked += 1
            if sticker.emotion in got:
                continue
            if alias in names:
                shadowed.append((alias, sticker.emotion))
                continue
            ok = False
            print(f"  x 别名 {alias!r} 漏了 {sticker.emotion!r}: {got}")
    if not ok:
        print("  x 别名声明者必须全部出现在候选里")
    print(f"  {'OK' if ok else 'x'} 真实 registry 别名完整性: {len(stickers)} 条 / {checked} 个别名"
          f"（其中 {len(shadowed)} 条被同名条目遮蔽: {shadowed[:4]}）")
    return ok


def test_mode_three_state() -> bool:
    """三态读数：未设置=off（逐字旧行为），on/1/true=on，shadow 原样，乱值回落 off。"""
    cases = ((None, "off"), ("off", "off"), ("shadow", "shadow"), ("on", "on"),
             ("1", "on"), ("true", "on"), ("yes", "on"), ("瞎写的", "off"))
    ok = True
    try:
        for value, expect in cases:
            _set_mode(value)
            for name, mod in (("tool", tool_mod), ("sender", sender_mod)):
                got = mod._sticker_candidates_mode()
                if got != expect:
                    ok = False
                    print(f"  x {name} env={value!r} → {got!r}（期望 {expect!r}）")
    finally:
        _set_mode(None)
    print(f"  {'OK' if ok else 'x'} 三态开关（选择侧 + 发送侧同源）: {len(cases)} 值 x 2 读点")
    return ok


def test_schema_gate() -> bool:
    """enum 只在 off/shadow 的 schema 里；on 换成"描述也行"，不承诺闭集。"""
    tool = tool_mod.SendStickerTool()
    ok = True
    try:
        for value, enum_expect in ((None, True), ("shadow", True), ("on", False)):
            _set_mode(value)
            prop = tool.schema()["function"]["parameters"]["properties"]["sticker"]
            has_enum = "enum" in prop
            if has_enum != enum_expect:
                ok = False
                print(f"  x env={value!r}: enum 存在={has_enum}（期望 {enum_expect}）")
            if not enum_expect and "描述" not in str(prop.get("description", "")):
                ok = False
                print(f"  x env=on 的 sticker 描述没说可以描述: {prop.get('description')!r}")
    finally:
        _set_mode(None)
    print(f"  {'OK' if ok else 'x'} schema 门控（off/shadow 与旧行为逐字一致）")
    return ok


def test_choose_candidate_and_text() -> bool:
    """歧义 → 弃权；唯一命中 → 择一；措辞档要给得出理由。"""
    a = _sticker("你说我吗？（呆呆的，装傻）", context="想装傻时使用",
                 label="卖萌", aliases=("发呆",))
    b = _sticker("喝奶茶，看戏", context="想喝奶茶看戏时使用", label="调皮", aliases=("发呆",))
    c = _sticker("点头", label="对", aliases=("对的",))
    ok = True
    with _entries_swapped([a, b, c]):
        report = tool_mod._candidate_report("发呆")
        if len(report["by_alias"]) != 2:
            ok = False
            print(f"  x 一对多候选: {len(report['by_alias'])} 条")
        chosen, decision = tool_mod._choose_candidate(report, "亲密")
        if chosen != "" or decision != "ambiguous":
            ok = False
            print(f"  x 歧义应弃权: ({chosen!r}, {decision!r})")
        text = tool_mod._candidate_error_text("发呆", report, decision)
        if "不止一个" not in text or "本轮不发" not in text:
            ok = False
            print(f"  x 歧义文案丢了弃权承诺: {text!r}")

        report = tool_mod._candidate_report("对的")
        chosen, decision = tool_mod._choose_candidate(report, "亲密")
        if chosen != "点头" or decision != "by_alias":
            ok = False
            print(f"  x 唯一别名应择一: ({chosen!r}, {decision!r})")

        report = tool_mod._candidate_report("喝奶茶看戏")
        chosen, decision = tool_mod._choose_candidate(report, "亲密")
        if decision != "by_wording" or chosen != "喝奶茶，看戏":
            ok = False
            print(f"  x 措辞档应能带来唯一候选: ({chosen!r}, {decision!r}) {report['by_wording']}")
    print(f"  {'OK' if ok else 'x'} 候选 → 证据 → 择一/弃权")
    return ok


def test_wording_matches_registered_name() -> bool:
    """差一个标点的名字也算措辞命中：请求「喝奶茶看戏」对上登记的「喝奶茶，看戏」。"""
    entry = _sticker("喝奶茶，看戏", context="", label="调皮")
    other = _sticker("已读不回", context="", label="无语")
    with _entries_swapped([entry, other]):
        report = tool_mod._candidate_report("喝奶茶看戏")
        chosen, decision = tool_mod._choose_candidate(report, "亲密")
    ok = chosen == "喝奶茶，看戏" and decision == "by_wording"
    if not ok:
        print(f"  x 名字进措辞场: ({chosen!r}, {decision!r}) {report.get('by_wording')}")
    print(f"  {'OK' if ok else 'x'} 措辞场覆盖登记名（标点差异不判死）")
    return ok


def test_hunt_outcome() -> bool:
    """「没搜完」和「翻遍了没有」必须给出不同的名字。"""
    panel3 = types.SimpleNamespace(tabs=[1, 2, 3])
    panel5 = types.SimpleNamespace(tabs=[1, 2, 3, 4, 5])
    full = [{"tab": tab, "page": page, "score": 0.3, "margin": 0.0}
            for tab in (0, 1) for page in range(10)]
    cases = (
        ({"ok": True, "match": types.SimpleNamespace(score=0.9123)}, panel3, "found", "score=0.912"),
        ({"ok": False, "trace": [{"result": "panel_lost"}]}, panel3, "incomplete", "panel_lost"),
        ({"ok": False, "trace": []}, panel3, "incomplete", "trace_empty"),
        ({"ok": False, "trace": [{"tab": 0, "page": 0, "cells": 0}]}, panel3, "incomplete", "no_lattice"),
        ({"ok": False, "trace": [{"tab": t, "page": 0, "score": 0.3} for t in (0, 1, 2)]},
         panel5, "incomplete", "tab_cap_hit(3/5)"),
        ({"ok": False, "trace": full}, types.SimpleNamespace(tabs=[1, 2]), "exhausted", "pages_read=20"),
    )
    ok = True
    for hunt, panel, expect, why in cases:
        got = sender_mod._hunt_outcome(hunt, panel)
        if got != (expect, why):
            ok = False
            print(f"  x _hunt_outcome → {got}（期望 {(expect, why)}）")
    print(f"  {'OK' if ok else 'x'} 搜索结局四分类 + 兜底: {len(cases)} 例")
    return ok


def test_refusal_is_not_absence() -> bool:
    """on 档：没搜完的文案里不许出现"没有这张/未找到"的断言。"""
    hunt = {"ok": False, "trace": [{"tab": 0, "page": 0, "cells": 0}]}
    panel = types.SimpleNamespace(tabs=[1, 2, 3])
    outcome, why = sender_mod._hunt_outcome(hunt, panel)
    text = sender_mod._hunt_failure_text(hunt, panel, outcome, why, "最佳相似度 0.40，低于阈值 0.45")
    ok = ("没走完" in text and "这不等于没有这张表情" in text
          and "未找到" not in text and "没有足够匹配" not in text)
    if not ok:
        print(f"  x 没搜完的文案: {text!r}")
    print(f"  {'OK' if ok else 'x'} 没搜完 != 不存在：{text[:40]}...")
    return ok


def test_live_registry_alias_uniqueness() -> bool:
    """真实 registry：一个别名只允许一个声明者，别名也不得等于另一条的名字。

    2026-10-16 数据侧清理后的锁（备份 backups/sticker_registry_aliases_20261016_210305）：
    谁先声明谁负责；重复声明在数据里删掉，代码不替数据做歧义裁决。
    """
    store = sticker_store.StickerStore("wechat", ACCOUNT)
    if not store.exists():
        print("  SKIP 真实 registry 不在（隔离环境）")
        return True
    stickers = list(store.load().values())
    names = {s.emotion for s in stickers}
    owners: dict[str, list[str]] = {}
    for sticker in stickers:
        for alias in sticker.aliases or ():
            owners.setdefault(alias, []).append(sticker.emotion)
    dupes = {a: w for a, w in owners.items() if len(w) > 1}
    shadowed = {a: w for a, w in owners.items() if a in names and a not in w}
    if dupes:
        print(f"  x 别名多主: {dupes}")
    if shadowed:
        print(f"  x 别名被同名条目遮蔽: {shadowed}")
    ok = not dupes and not shadowed
    print(f"  {'OK' if ok else 'x'} 真实 registry 别名唯一性: {len(stickers)} 条 / {len(owners)} 个别名")
    return ok


TESTS = (
    test_looks_encoded,
    test_description_lens,
    test_match_candidates_one_to_many,
    test_candidates_shape,
    test_live_registry_alias_completeness,
    test_mode_three_state,
    test_schema_gate,
    test_choose_candidate_and_text,
    test_wording_matches_registered_name,
    test_hunt_outcome,
    test_refusal_is_not_absence,
    test_live_registry_alias_uniqueness,
)


def main() -> int:
    results = []
    for test in TESTS:
        print(f"[{test.__name__}]")
        try:
            ok = bool(test())
        except Exception as exc:  # noqa: BLE001
            ok = False
            import traceback
            traceback.print_exc()
            print(f"  x 异常: {type(exc).__name__}: {exc}")
        results.append((test.__name__, ok))
    print()
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    failed = [n for n, ok in results if not ok]
    print(f"sticker candidate checks: {len(results) - len(failed)}/{len(results)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
