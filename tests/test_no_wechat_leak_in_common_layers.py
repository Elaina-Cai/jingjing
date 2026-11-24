# -*- coding: utf-8 -*-
"""跨平台层守卫：**"判群聊"这个动作不许在通用层新增第二处实现**。

为什么
======
群聊判定的唯一权威是 ``core.identity.platforms``（``chat_type_for`` / ``is_group_chat``：
canonical ``chat_type`` 优先，平台后缀只是 legacy 兜底）。它的模块头写着：

    Generic identity code must not know that WeChat groups end with ``@chatroom``

2026-10-10（GAP-2）实测：通用层里有若干处**自己解析** ``chat_id.endswith("@chatroom")``。
其中两处已改成走权威函数：

* ``agents/tools/send_sticker.py`` —— 函数签名里本来就有 ``platform``
* ``core/contracts/legacy.py`` —— ``chat_type_for`` 就是为这个场景设计的

最后那处顺带修了一个**潜在** bug：旧写法 ``GROUP if ... else PRIVATE`` 会把 **QQ 群**
（``group:123``）判成私聊。现状下没触发——生产库里 QQ 只有 6 条事件、1 个会话，
且是私聊（``user:1768136519``）；``group:`` 前缀还一条都没有。

为什么不零容忍
==============
剩下的 3 处**各自有理由**（见 ``EXEMPT``），而且通用层里 ``"wechat"`` 作为默认值有 15 处
——那是合理的（当前只跑微信），不是渗漏。**本守卫只锁"判群聊的形态"**，不锁平台名。

守卫方式：**冻结基线、只减不增**。基线**按符号名登记**（不是按文件路径），
所以拆大文件、把实现搬走不会假红；只有「新开一处判群聊的形态」才会红——
口径见 ``EXEMPT`` 上方那段注释（键唯一性 / 只数符号体内 / 保留 stale）。
"""

from __future__ import annotations

import io
import os
import re
import tokenize
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 扫的"通用层"：不属于微信适配器的那些目录。
TARGETS = ("agents/tools", "core/contracts", "core/speech", "core/ingestion")

# 只锁**平台 id 形态**，不锁平台名（"wechat" 作默认值到处都是，那是合理的）。
SHAPE_WORDS = ("@chatroom",)

# 冻结基线：**按符号名**而不是按文件路径登记（2026-10-22 改）。
#
# 为什么改键：`c768143` 把 `proactive_target_id` 从 outbound.py 搬到 proactive_ledger.py，
# 旧的路径键当场 stale 变红；可那只是**搬家**，一次「判群聊的形态」都没多。路径键把
# 「谁在实现」和「实现在哪」混成了一件事，于是每次拆大文件都要改守卫。
# 现在键是**符号名**：只要那个函数还叫这个名字、还只定义一处，搬家就不会红。
#
# 三条口径（缺一不可）：
# 1. 键是全仓**恰好只定义一次**的符号名（`Class.method` 用点号限定）；找不到或重名都红
#    ——重名意味着「这个键指向哪个实现」是含糊的。唯一例外：写成 `路径::名字` 时只要求
#    **该文件内**唯一（`_looks_like_chat_id` 这种名字全仓就有两处，各自文件内唯一）。
# 2. 计数口径 = 落在该符号体**之内**的命中数与冻结值比；符号体**之外**的命中一律为 0。
#    也就是说：豁免的是「这个函数里这一处」，不是「这个文件里几处」。
# 3. 保留 stale：符号消失 / 解析不到 / 体重数变 0 都要红——豁免项必须能自证「仍需要」。
EXEMPT: dict[str, tuple[int, str]] = {
    "SendStickerTool._looks_like_chat_id": (
        1, "`_looks_like_chat_id`：判「像不像一个原始 id」的**宽容匹配**，不是群聊判定——多认不漏；"
           "真正的群聊判定已改走 core.identity.platforms（core/execution/wx_chat.py 也有个模块级同名函数，但它没有这处形态；用 `类名.方法名` 限定即唯一）"),
    "proactive_target_id": (
        1, "上一行已 `if getattr(target_obj, 「platform」, 「」) != 「wechat」: return`，**平台已限定**；"
           "且本处是「像不像一个原始 id」的宽容匹配（多认不漏），不是群聊判定"
           "（`c768143` 把它从 outbound.py 搬到这里，**本键随符号走、无需再改**）"),
    "WechatMessage.is_group": (
        1, "本类就是**微信采集适配器**（其 msg_type 走 `PlatformTypeMapper.from_wechat`），"
           "平台事实写在这里正是它该在的地方"),
}


def _blank_comments(text: str) -> list[str]:
    R"""把注释字符抹成空格，**保持行号与列号不变**（只留代码 + 字符串字面量）。

    为什么必须剥注释：注释里写「微信群 id 以 @chatroom 结尾」是有益的文档，不该算违规。
    为什么不能像老版本那样把 token 拼起来：那会重排行号，而本守卫要按**行号**把命中
    归到符号体（内/外），行号必须和 AST 的 lineno / end_lineno 同一坐标系。
    """
    import io
    import tokenize

    lines = text.splitlines()
    try:
        spans = [
            (tok.start[0], tok.start[1], tok.end[0], tok.end[1])
            for tok in tokenize.generate_tokens(io.StringIO(text).readline)
            if tok.type == tokenize.COMMENT
        ]
    except Exception:  # noqa: BLE001 - 语法不完整时退回全文（宁可多报）
        return lines
    for srow, scol, erow, ecol in spans:
        if srow == erow and 0 < srow <= len(lines):
            line = lines[srow - 1]
            lines[srow - 1] = line[:scol] + " " * max(0, ecol - scol) + line[ecol:]
    return lines


_DEFS_CACHE: dict[str, list[tuple[str, int, int]]] = {}


def _definitions() -> dict[str, list[tuple[str, int, int]]]:
    """全仓所有函数/方法的限定名 -> [(相对路径, 起行, 止行)]。

    限定名 = 外层类/函数的点号链（模块级就是函数名）。同名不同文件在这里**不合并**，
    由 `_resolve` 决定「按全仓唯一」还是「按单文件唯一」。
    """
    import ast

    skip = {".venv", ".venv_it", ".git", ".test_tmp", ".tmp_test", "output",
            "backups", "data", "logs", "node_modules", "__pycache__", "_scratch"}
    out: dict[str, list[tuple[str, int, int]]] = {}

    def visit(node, stack: list[str], rel: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = ".".join(stack + [child.name])
                if isinstance(child, ast.ClassDef):
                    visit(child, stack + [child.name], rel)
                else:
                    out.setdefault(qual, []).append((rel, child.lineno, child.end_lineno))
                    visit(child, stack + [child.name], rel)
            else:
                visit(child, stack, rel)

    # 唯一性要在全仓范围成立，但没必要解析全仓：只有提到这些名字的文件才可能是定义处
    # （纯文本预筛，把 1000+ 个文件的 AST 解析压到个位数，单跑从 30 秒降到 1 秒量级）。
    needles = [k.split("::")[-1].split(".")[-1].encode() for k in EXEMPT]
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 剪枝而不是事后过滤：`.venv/` 与 `.test_tmp/`（全量测试的临时目录，几万个文件）
        # 一旦走进去，光遍历就要十几秒。
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if not any(n in raw for n in needles):
                continue
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")  # 别人的无效转义不该刷屏
                    tree = ast.parse(raw.decode("utf-8-sig", "replace"))
            except Exception:  # noqa: BLE001 - 解析不了的文件不参与（守卫只关心能解析的源码）
                continue
            visit(tree, [], rel)
    _DEFS_CACHE.update(out)
    return _DEFS_CACHE


def _resolve(key: str, defs: dict[str, list[tuple[str, int, int]]]) -> tuple[str, int, int]:
    """解析一个豁免键 -> (相对路径, 起行, 止行)。找不到 / 含糊时抛 ValueError。"""
    if "::" in key:
        rel, name = key.split("::", 1)
        cands = [d for d in defs.get(name, []) if d[0] == rel]
        if not cands:
            raise ValueError(f"找不到符号 {name}（限定到 {rel}）")
        if len(cands) > 1:
            raise ValueError(f"{rel} 里 {name} 定义了 {len(cands)} 次，键含糊")
        return cands[0]
    cands = defs.get(key, [])
    if not cands:
        raise ValueError(f"找不到符号 {key}（全仓）")
    if len(cands) > 1:
        paths = ", ".join(sorted({d[0] for d in cands}))
        raise ValueError(f"{key} 在全仓定义了 {len(cands)} 次（{paths}），键含糊：请写成 `路径::名字`")
    return cands[0]


def _scan() -> tuple[dict[str, int], list[str], list[str]]:
    """扫一遍通用层。

    返回 `(每个豁免符号体内的次数, 符号体外的命中, 键解析错误)`。命中按**行号**归到符号体：
    落在某个已登记符号的 `lineno..end_lineno` 之内就算它的，否则算「体外」。
    """
    defs = _definitions()
    spans: dict[str, tuple[str, int, int]] = {}
    errors: list[str] = []
    for key in EXEMPT:
        try:
            spans[key] = _resolve(key, defs)
        except ValueError as exc:  # noqa: BLE001 - 键失效要报告，不是崩溃
            errors.append(f"{key}：{exc}")
    outside: list[str] = []
    inside: dict[str, int] = {key: 0 for key in EXEMPT}
    for target in TARGETS:
        base = ROOT / target
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            for line_no, line in enumerate(_blank_comments(path.read_text(encoding="utf-8")), 1):
                for word in SHAPE_WORDS:
                    for _ in range(line.count(word)):
                        owner = next(
                            (k for k, (srel, start, end) in spans.items()
                             if srel == rel and start <= line_no <= end),
                            None,
                        )
                        if owner is None:
                            outside.append(f"{rel}:{line_no}: {word}")
                        else:
                            inside[owner] += 1
    return inside, outside, errors


def test_group_detection_does_not_spread_in_common_layers():
    """通用层里「判群聊的形态」只许待在已登记的符号体里；符号体外一处都不许有。"""
    inside, outside, errors = _scan()
    assert not errors, (
        "豁免键解析失败——键必须是全仓恰好定义一次的符号名（重名用 `路径::名字` 限定）：\n  "
        + "\n  ".join(errors)
    )
    assert not outside, (
        "通用层出现**符号体之外**的「判群聊的形态」——群聊判定的唯一权威是 core.identity.platforms"
        "（chat_type_for / is_group_chat）：\n  "
        + "\n  ".join(outside)
        + "\n\n不要自己解析 chat_id 后缀（微信是 @chatroom、QQ 是 group: 前缀）。"
        "\n若确属有意，在 EXEMPT 里登记**该实现所在的符号名**并写明理由（符号体内次数也只减不增）。"
    )
    grew = [
        f"{key}: 冻结 {allowed} -> {inside[key]}"
        for key, (allowed, _why) in EXEMPT.items()
        if inside[key] > allowed
    ]
    assert not grew, (
        "已登记符号体里的「判群聊的形态」变多了（冻结值只减不增）：\n  " + "\n  ".join(grew)
        + "\n\n同一个符号里多出一处，说明它开始兼做第二件事：请把「像不像原始 id」的判断"
        "和「是不是群聊」的判断分开写。"
    )
    return True


def test_exempt_entries_are_still_needed():
    """豁免表只许减少：符号没了、或符号体里再没有该形态，都要从 EXEMPT 摘掉。"""
    inside, _outside, errors = _scan()
    stale = [f"{key}（符号体里 0 处）" for key in EXEMPT if inside.get(key, 0) == 0]
    assert not errors and not stale, (
        "这些豁免已经不需要了，请从 EXEMPT 摘掉：" + str(stale or errors)
    )
    return True

if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)