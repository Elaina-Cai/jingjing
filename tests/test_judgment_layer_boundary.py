# -*- coding: utf-8 -*-
"""判定层边界守卫：平台名与具体业务动作不许在 core/routing、core/context 里**增加**。

为什么
======
``core/routing/`` 与 ``core/context/`` 是**判定层**——它只该处理通用概念（谁是说话人、
这句话冲谁来、该不该开口），不该知道「微信」「拍一拍」「表情包」这些**平台或产品**的东西。
那属于 World / 适配层。

为什么不是零容忍
================
2026-10-22 实测：判定层里**仍有 86 处**（平台名 42 + 业务动作 44），分布在 16 个文件
（2026-10-10 首次冻结时是 67 处 = 23 + 44）。它们是从早期长出来的，一次性清零会把风险
摊在一次大改动里。所以本用例**冻结现状**，口径是**双向精确**：

* 某个 ``(文件, 词)`` 计数**不得超过**基线（增长 ⇒ 红）；
* 基线里**登记过的** ``(文件, 词)`` 必须**文件仍在、次数仍精确相等**——**少了也红**；
* 基线里**没有**的文件里出现禁词 ⇒ 红（新代码不许再带进来）。

为什么连「少了」也要红：这个用例的语义是「基线 = 现状」。只防增长而放任缩水，基线会
长期虚高；虚高之后新代码在同一个文件里再带进一处词**根本不会红**——预算被前人的撤离
吃掉了。表里记的东西和现实脱钩、脱钩时没人被叫醒，这和「守卫按文件路径登记豁免」
是同一种病。减少本身是好事，但必须解释（可能是搬回 World 层、也可能是搬到了 scope
之外、还可能是改成了同义词——三种含义要分开确认）。

基线：``tests/golden/judgment_layer_forbidden_baseline.json``
有意减少之后重新生成（**降账也要在 ``docs/控制元规则.md`` 里写一句为什么少**）::

    python scripts/gen_boundary_baseline.py --check     # 双向核对，不一致即退 1
    python scripts/gen_boundary_baseline.py --write     # 写盘（有增长时拒绝）
    python scripts/gen_boundary_baseline.py --write --allow-growth   # 授权增长（会打印增/减两侧）

不在此列的词
============
「群聊 / 私聊 / is_group / chatroom」是**通用 IM 概念**，判定层本来就要处理它们——
禁掉会把判定层逼成什么都不能说。本用例只禁**平台名**与**具体业务动作**。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "tests" / "golden" / "judgment_layer_forbidden_baseline.json"


def _load() -> tuple[list[str], list[str], dict]:
    assert BASELINE_PATH.exists(), (
        f"基线缺失：{BASELINE_PATH}——用 scripts/gen_boundary_baseline.py --write 生成"
    )
    payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    words = list(payload["_platform_words"]) + list(payload["_business_words"])
    return list(payload["_scope"]), words, dict(payload["baseline"])


def _scan() -> tuple[dict, dict]:
    scope, words, baseline = _load()
    actual: dict[str, dict[str, int]] = {}
    for target in scope:
        for path in sorted((ROOT / target).rglob("*.py")):
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            text = path.read_text(encoding="utf-8")
            for word in words:
                hits = len(re.findall(re.escape(word), text, re.IGNORECASE))
                if hits:
                    actual.setdefault(rel, {})[word] = hits
    return baseline, actual


def test_judgment_layer_forbidden_words_do_not_grow():
    """任何 (文件, 词) 超过基线、或新文件带进来 → 红。"""
    baseline, actual = _scan()
    grew: list[str] = []
    for rel, words in actual.items():
        allowed_map = baseline.get(rel, {})
        for word, hits in words.items():
            allowed = int(allowed_map.get(word, 0))
            if hits > allowed:
                grew.append(f"{rel}: 「{word}」 {allowed} -> {hits}")
    assert not grew, (
        "判定层新增了平台/业务词（core/routing、core/context 只该处理通用概念）：\n  "
        + "\n  ".join(grew)
        + "\n\n判定层不该知道「微信」「拍一拍」这类平台或产品概念——它们属于 World / 适配层。"
        + "\n若确属有意，先改 docs/控制元规则.md 的白名单，再跑 "
        + "scripts/gen_boundary_baseline.py --write 更新基线。"
    )
    return True


def test_baseline_entries_match_reality_exactly():
    """基线登记的 (文件, 词) 必须仍在、次数仍精确相等——**减少也算 stale 红**。

    只防增长而放任缩水，基线会长期虚高；虚高之后同一个文件里新增一处词根本不会红
    （预算被前人的撤离吃掉了）。减少是好事，但要人来解释，不能静默。
    """
    _baseline, actual = _scan()
    baseline = _baseline
    stale: list[str] = []
    for rel in sorted(baseline):
        if not (ROOT / rel).is_file():
            total = sum(int(v) for v in baseline[rel].values())
            stale.append(f"{rel}: 文件已不存在（基线里登记了 {total} 处，改归/删文件？）")
            continue
        for word in sorted(baseline[rel]):
            want = int(baseline[rel][word])
            got = int(actual.get(rel, {}).get(word, 0))
            if got < want:
                stale.append(f"{rel}: 「{word}」 登记 {want}、实际 {got}（{got} < {want}）")
    assert not stale, (
        "判定层禁词基线比现实虚高——登记的次数已经不存在了：\n  "
        + "\n  ".join(stale)
        + "\n\n减少不是坏事，但必须解释：它可能是 ① 搬回 World/适配层或直接删掉（好）；"
        + "\n② 搬到了 scope 之外（core/routing、core/context 以外）——要确认那里有没有守卫；"
        + "\n③ 改写成同义词——要确认新词进没进禁词表。"
        + "\n\n修法：① 在 docs/控制元规则.md 补一句「为什么少了」；"
        + "\n      ② python scripts/gen_boundary_baseline.py --write 把基线降到实况；"
        + "\n      ③ 若同轮还有增长，用 --write --allow-growth（它会同时打印增/减两侧明细，"
        + "提交信息逐条对应）。"
    )
    return True


def test_baseline_file_is_intact():
    """基线文件自身的完整性：禁词表非空、scope 指向存在的目录、基线非空。"""
    scope, words, baseline = _load()
    assert words, "禁词表为空"
    assert len(scope) >= 2, "scope 太少"
    for target in scope:
        assert (ROOT / target).is_dir(), f"scope 指向不存在的目录：{target}"
    assert baseline, "基线为空"
    return True


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
