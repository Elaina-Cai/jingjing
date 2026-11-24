# -*- coding: utf-8 -*-
"""生成 / 校验判定层禁词基线（``tests/golden/judgment_layer_forbidden_baseline.json``）。

配套用例：``tests/test_judgment_layer_boundary.py``。口径只有一份——那就是基线文件本身，
本脚本只负责扫描与写盘。

用法::

    python scripts/gen_boundary_baseline.py            # 只报告差异，不写盘
    python scripts/gen_boundary_baseline.py --check    # 与磁盘基线逐条核对（CI/体检用）
    python scripts/gen_boundary_baseline.py --write    # 写盘（**只减不增**）
    python scripts/gen_boundary_baseline.py --write --allow-growth   # 授权后连增长一起写

``--write`` 的硬约束
--------------------
发现任何 ``(文件, 词)`` 比现基线**增长**时**拒绝写入**，并列出明细。
理由：这个基线是"冻结现状"的守卫，能被随手刷绿就失去意义。
要合法增加，先改 ``docs/控制元规则.md`` 的白名单，再谈基线。

``--allow-growth`` 是那条路的机械闸：**``docs/控制元规则.md`` 不存在就直接拒绝**
（不能只凭一个命令行开关把守卫刷绿），存在才允许写入，并把每一处增长逐条打印出来
——提交信息里必须能对上这些行。

``--allow-growth`` 同时承担**降账入口**：它打印的是**增/减两侧**明细（``增 N 处 / 减 M 处``）。
为什么降账也要走这里：配套用例 ``tests/test_judgment_layer_boundary.py`` 现在是**双向精确**
口径（少了也红），因为只防增长会放任基线虚高——虚高之后新词根本不会红。纯减少的
``--write`` 仍然允许（减少随时欢迎），但会打印一行提醒：**降账也要在
``docs/控制元规则.md`` 里记一句为什么少**（搬回 World 层 / 搬到 scope 外 / 改成同义词，
三种含义要分开确认）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "tests" / "golden" / "judgment_layer_forbidden_baseline.json"

TARGETS = ["core/routing", "core/context"]

# 平台名：判定层不该知道跑在哪个平台上（那是 World / 适配层的事）。
PLATFORM_WORDS = ["wechat", "微信", "napcat", "onebot", "telegram", "企业微信", "企微", "qq"]
# 具体业务动作：这些是**产品功能**，不是通用判定概念。
BUSINESS_WORDS = ["拍一拍", "戳一戳", "表情包", "贴纸", "提醒", "日程", "待办",
                  "投票", "接龙", "红包", "转账", "朋友圈"]


def scan() -> dict[str, dict[str, int]]:
    """按 (文件, 词) 统计命中数。"""
    words = PLATFORM_WORDS + BUSINESS_WORDS
    actual: dict[str, dict[str, int]] = {}
    for target in TARGETS:
        for path in sorted((ROOT / target).rglob("*.py")):
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            text = path.read_text(encoding="utf-8")
            for word in words:
                hits = len(re.findall(re.escape(word), text, re.IGNORECASE))
                if hits:
                    actual.setdefault(rel, {})[word] = hits
    return actual


def load_baseline() -> dict[str, dict[str, int]]:
    if not BASELINE_PATH.exists():
        return {}
    return dict(json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["baseline"])


def diff(base: dict, actual: dict) -> tuple[list[str], list[str]]:
    """返回 (增长明细, 减少明细)。新增文件带词也算增长。"""
    grew: list[str] = []
    shrank: list[str] = []
    for rel in sorted(set(base) | set(actual)):
        base_words = base.get(rel, {})
        actual_words = actual.get(rel, {})
        for word in sorted(set(base_words) | set(actual_words)):
            before = int(base_words.get(word, 0))
            after = int(actual_words.get(word, 0))
            if after > before:
                grew.append(f"{rel}: 「{word}」 {before} -> {after}")
            elif after < before:
                shrank.append(f"{rel}: 「{word}」 {before} -> {after}")
    return grew, shrank


def vanished(base: dict, actual: dict) -> list[str]:
    """从 N 降到 0 的条目。

    这类条目在写盘时**整条从基线里消失**（不是数字变 0），是唯一真正「丢信息」的缩水：
    以后这个词再出现，会按「新文件 / 新词」重新记账，看不出「它曾经在过这里」。
    所以 `--write` 要单独把它们念一遍，让人知道刚抹掉了什么。
    """
    gone: list[str] = []
    for rel in sorted(base):
        for word in sorted(base[rel]):
            before = int(base[rel][word])
            if before and not int(actual.get(rel, {}).get(word, 0)):
                gone.append(f"{rel}: 「{word}」 {before} -> 0")
    return gone


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="判定层禁词基线")
    parser.add_argument("--write", action="store_true", help="写盘（只减不增）")
    parser.add_argument("--check", action="store_true", help="与磁盘基线逐条核对")
    parser.add_argument(
        "--allow-growth", action="store_true",
        help="授权后连增长一起写（前提：docs/控制元规则.md 存在）",
    )
    args = parser.parse_args(argv)

    base = load_baseline()
    actual = scan()
    grew, shrank = diff(base, actual)
    total = sum(sum(v.values()) for v in actual.values())
    print(f"判定层禁词：{len(actual)} 个文件 / {total} 处")

    if grew:
        print("\n增长（必须处理）：")
        for line in grew:
            print("  + " + line)
    if shrank:
        print("\n减少（好事，可 --write 固化）：")
        for line in shrank:
            print("  - " + line)
    if not grew and not shrank:
        print("与基线完全一致。")
    elif grew or shrank:
        print(f"\n小结：增 {len(grew)} 处 / 减 {len(shrank)} 处（基线 = 现状，两侧都算不一致）")

    if args.check:
        return 1 if (grew or shrank) else 0

    if args.write:
        if grew and not args.allow_growth:
            print("\n拒绝写入：有增长。先改 docs/控制元规则.md 的白名单，再谈基线。")
            return 1
        if grew and args.allow_growth:
            rule_doc = ROOT / "docs" / "控制元规则.md"
            if not rule_doc.exists():
                print(
                    "\n拒绝写入：--allow-growth 需要先有 docs/控制元规则.md"
                    "（写清白名单与本轮增长的逐条理由）。"
                )
                return 1
            print(
                f"\n按 --allow-growth 写入：增 {len(grew)} 处 / 减 {len(shrank)} 处"
                "（两侧都要在 docs/控制元规则.md 与提交信息里逐条对应）："
            )
            for line in grew:
                print("  + " + line)
            for line in shrank:
                print("  - " + line)
        elif shrank:
            print(
                "\n注意：本轮只有减少。减少随时欢迎，但**降账也要解释**——"
                "请在 docs/控制元规则.md 里记一句为什么少（搬回 World 层 / 搬到 scope 外 / 改成同义词）。"
            )
            for line in shrank:
                print("  - " + line)
        gone = vanished(base, actual)
        if gone:
            print(
                "\n条目消失（真正丢信息：以后它们再出现，会按「新文件 / 新词」重新记账，"
                "看不出「曾经在过这里」）："
            )
            for line in gone:
                print("  x " + line)

        payload = {
            "_comment": "判定层禁词基线（只减不增）。生成：python scripts/gen_boundary_baseline.py --write",
            "_scope": TARGETS,
            "_platform_words": PLATFORM_WORDS,
            "_business_words": BUSINESS_WORDS,
            "baseline": actual,
        }
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写入 {BASELINE_PATH}")
        return 0

    return 1 if grew else 0


if __name__ == "__main__":
    raise SystemExit(main())
