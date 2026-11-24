# -*- coding: utf-8 -*-
"""prompt 只由「四层」构成——归属表守卫（2026-10-23 主人裁定后建）。

主人要的是：提示词里不再有「旧方案时期长出来、谁也不知道它属于哪一层」的块。
实现方式不是一次大扫除（扫完还会再长），而是**把层归属变成机器可查的账本**：
``core/context/prompt_layers.py::BLOCK_LAYER`` 是唯一账本，本文件要求
``core/context/assembler.py::_BLOCK_POLICY`` 里**每一条登记都必须恰好属于某一层**。

于是「新增一块」这个动作被强制多问一句：它喂的是哪一层？想不清楚 → 单跑就红。
这比「写进文档请遵守」结实，因为文档没人被迫读。

Run directly:
    python tests/test_prompt_layers.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context import assembler                                # noqa: E402
from core.context import prompt_layers as pl                      # noqa: E402

COUNT = 0
FAILS: list[str] = []

#: 不属于 `_BLOCK_POLICY` 登记（由调用方直接贴进 system prompt），但仍需归层。
DYNAMIC_BLOCKS: dict[str, str] = {
    # `agents/conversational.py::_self_continuity_block` 贴的「自我连续性」
    "[你说过·作数]": pl.PERSONA,
    # `loop/brain.py` 直接贴进 user 消息的「现场补充」第四段（brain 不走 assembler，
    # 所以它不在 `_BLOCK_POLICY` 里），2026-11-03 接上。
    "[未完的事]": pl.MEMORY,
}


def check(name: str, ok: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    print(f"[{'ok' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if (detail and not ok) else ""))
    if not ok:
        FAILS.append(f"{name} :: {detail}")


def _registered_prefixes() -> tuple[str, ...]:
    return tuple(str(entry[0]) for entry in assembler._BLOCK_POLICY)


def t_every_registered_block_has_a_layer() -> None:
    missing = [p for p in _registered_prefixes() if not pl.layer_of(p)]
    check(
        "每一条登记的块都能说出它属于哪一层",
        not missing,
        "没有归属的块: "
        + ", ".join(missing)
        + " —— 去 core/context/prompt_layers.py 给它指定一层；"
        "如果指不出来，说明它不该继续存在",
    )


def t_no_fictional_entries() -> None:
    registered = set(_registered_prefixes())
    known = registered | set(DYNAMIC_BLOCKS)
    bogus = [p for p in pl.BLOCK_LAYER if p not in known]
    check(
        "账本里没有「登记了但仓库不存在」的块",
        not bogus,
        "账本多出来的: " + ", ".join(bogus) + "（删了块就同步删账本，否则账本会永久虚高）",
    )


def t_dynamic_blocks_are_declared_with_a_layer() -> None:
    bad = [p for p, layer in DYNAMIC_BLOCKS.items() if pl.layer_of(p) != layer]
    check(
        "动态贴进来的块也在账本里、且层与声明一致",
        not bad,
        "不一致: " + ", ".join(bad),
    )


def t_four_layers_all_populated() -> None:
    check("恰好四层", len(pl.LAYERS) == 4, f"实际 {len(pl.LAYERS)} 层")
    empty = [layer for layer in pl.LAYERS if not pl.blocks_of(layer)]
    check("四层都不是空壳", not empty, "没有块的层: " + ", ".join(empty))


def t_retired_block_stays_deleted() -> None:
    # `[性格与表达]` 是 2026-10-23 删掉的死登记（只登记、从来没产出过），不许回潮。
    check(
        "[性格与表达] 不再作为块出现",
        "[性格与表达]" not in _registered_prefixes() and not pl.layer_of("[性格与表达]"),
        "它又在登记表或账本里出现了",
    )


def t_table_is_pure() -> None:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "context", "prompt_layers.py")
    text = open(path, encoding="utf-8").read()
    bad = [word for word in ("import sqlite3", "import structlog", "core.context.intent",
                             "from llm", "open(") if word in text]
    check("账本模块是纯数据（不拉任何运行期依赖）", not bad, "出现了: " + ", ".join(bad))


def t_layer_lookup_is_total() -> None:
    check("未登记的块头 → 空串（守卫据此判红）", pl.layer_of("[不存在的块]") == "" and pl.layer_of("") == "")


def t_retired_blocks_are_really_gone() -> None:
    """退役的块：不许再登记、不许再进账本、产者源码里也不许再有这个块头。"""
    registered = set(_registered_prefixes())
    still_registered = [b for b in pl.RETIRED_BLOCKS if b in registered]
    check("退役块不再出现在登记表", not still_registered, ", ".join(still_registered))
    still_ledgered = [b for b in pl.RETIRED_BLOCKS if pl.layer_of(b)]
    check("退役块不再占层账本", not still_ledgered, ", ".join(still_ledgered))

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    producers = [
        os.path.join(root, "core", "terminal", "persona_brain.py"),
        os.path.join(root, "core", "context", "assembler.py"),
    ]
    texts = {path: open(path, encoding="utf-8").read() for path in producers}
    leftovers = []
    for block in pl.RETIRED_BLOCKS:
        for path, text in texts.items():
            if block in text:
                leftovers.append(f"{os.path.basename(path)}:{block}")
    check("产者源码里不再有退役块头", not leftovers, ", ".join(leftovers))


def main() -> int:
    for fn in (
        t_every_registered_block_has_a_layer,
        t_no_fictional_entries,
        t_dynamic_blocks_are_declared_with_a_layer,
        t_four_layers_all_populated,
        t_retired_block_stays_deleted,
        t_retired_blocks_are_really_gone,
        t_table_is_pure,
        t_layer_lookup_is_total,
    ):
        print(fn.__name__)
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            import traceback

            traceback.print_exc()
            FAILS.append(f"{fn.__name__} raised {exc!r}")
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    for item in FAILS:
        print(f"  - {item}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())