# -*- coding: utf-8 -*-
"""文本文件的**写回契约**：统一行尾、去尾部空行、按字节限大小、先跑 blockers。

为什么要有它（CB-069，来自 surfcore `src/preset-prompt.js:129-150`）：
人设卡 11,722 字那天离 20,000 上限只差 8,278 字，而 `loop/selfcard.py` 是**读到超限就裁掉尾巴**——
她读到的是一张被腰斩的卡，日志里一条 warn，然后没人知道。这类"上限"如果只在读的时候生效，
它就不是约束，是静默损坏。

所以契约放在这一处，读和写都必须过它：
- `normalize()` 决定"写下来的字节长什么样"（读写同一套，不各写一遍）；
- `blockers()` 决定"什么算不合格"，返回**可读的原因**，不返回布尔；
- `save()` 先跑 blockers，有一条不合就抛 `ContractError`，**一个字节都不写**；
- `load()` 不裁切、不猜，把原始内容和违规清单一起交给调用方。

规矩（开发规矩 §2.11 禁枚举）：上限只在 `Contract` 里写一次，别处不许再出现第二个数。
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

from core.infra.jsonio import atomic_write_text


class ContractError(Exception):
    """写回被契约拒绝（原因在消息里，人能照着改）。"""


@dataclass(frozen=True)
class Contract:
    """一个文本文件的形状。上限只有这里一份。"""

    name: str
    max_bytes: int
    head_keys: tuple[str, ...] = ()
    head_nonempty: tuple[str, ...] = ("name",)
    head_terminator: str = "---"
    note: str = ""


def normalize(text: str) -> str:
    """把行尾统一成 \\n、去掉尾部空行，**中间行一个字都不动**。

    只有空格/Tab 的行必须原样保留：YAML 会把它们当空行读回，删了就变成另一种文件。
    """
    out = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = out.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def blockers(raw: str, contract: Contract) -> list[str]:
    """这份内容违反了契约哪几条（空列表 = 合格）。给人看的原因，不是布尔。"""
    bad: list[str] = []
    if "\r" in raw:
        bad.append("行尾不统一（含 \\r，应该是 \\n）")
    if raw and not raw.endswith("\n"):
        bad.append("文件末尾没有换行")
    elif raw.endswith("\n\n"):
        bad.append("尾部有多余空行")
    norm = normalize(raw)
    size = len(norm.encode("utf-8"))
    if size > contract.max_bytes:
        bad.append("文件 %d 字节，超过上限 %d（多 %d 字节）"
                   % (size, contract.max_bytes, size - contract.max_bytes))
    if contract.head_keys:
        lines = norm.split("\n")
        term = contract.head_terminator
        idx = next((i for i, ln in enumerate(lines[:60]) if ln.strip() == term), -1)
        if idx < 0:
            bad.append("找不到头部分隔行 %r" % term)
        else:
            head = {}
            for ln in lines[:idx]:
                if ":" in ln:
                    k, _, v = ln.partition(":")
                    head[k.strip()] = v.strip()
            for key in contract.head_keys:
                if key not in head:
                    bad.append("头部缺 %s" % key)
                elif key in contract.head_nonempty and not head[key]:
                    # 只有"必须有序号/身份"的键才要求非空；voice/looks 留空是**合法配置**
                    # （空 = 用默认），逼人填值会把配置选择变成违规。
                    bad.append("头部 %s 是空的" % key)
            if not "\n".join(lines[idx + 1:]).strip():
                bad.append("正文是空的（只剩头部）")
    return bad


def load(path: str, contract: Contract) -> tuple[str, list[str]]:
    """读出文件与它的违规清单。**不裁切**：裁切会让"超限"变成看不见的事实。"""
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    return raw, blockers(raw, contract)


def save(path: str, text: str, contract: Contract) -> str:
    """合规才写：先跑 blockers，有一条不合就抛，一个字节都不动。"""
    clean = normalize(text)
    bad = blockers(clean, contract)
    if bad:
        raise ContractError("%s 不符合「%s」契约：%s" % (
            os.path.basename(path), contract.name, "；".join(bad)))
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak_contract-" + time.strftime("%Y%m%d-%H%M%S"))
    atomic_write_text(path, clean)
    return path
