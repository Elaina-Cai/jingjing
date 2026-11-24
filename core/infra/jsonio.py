# -*- coding: utf-8 -*-
# JSON 状态文件的唯一读写口：写要原子，读坏了要出声。
#
# 对应 docs/该抄的清单.md 的 BR-007（原子写）与 CA-057（坏数据回默认，绝不把坏数据当不限）。
#
# 为什么要有这一层：
# - open(path, "w") 直写：断电、崩溃或两个进程同时写，文件只剩半截 JSON；读侧再 except: return {}，
#   数据就是无声蒸发，不是报错。
# - 读侧静默回默认：坏数据和「本来就没有」长得一模一样，故障隐身。
#
# 两条规矩：
# 1. 写：先写同目录临时文件，再 os.replace 换上去（Windows 上「先删再改名」不原子，os.replace 才是原子的）；
#    被占用时退避重试，失败只记日志并返回 False，要不要紧由调用方决定。
# 2. 读：解析失败时把坏文件备份成 <name>.bad-<时间戳>（同一份文件只备份一次），
#    返回调用方给的默认值，并打一条 warning 说明用了默认值。
# 3. 要自己决定「坏了要不要收紧」的调用方用 read_json_checked：它把缺少和坏分开报。
#
# 这里不做业务判断（「坏数据算 0 还是算不限」由调用方传的默认值决定）。
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["atomic_write_json", "atomic_write_text", "read_json", "read_json_checked"]


def atomic_write_text(path, text: str, *, retries: int = 4) -> bool:
    # 原子写文本。返回是否写成（失败只记日志，不抛）。
    p = Path(path)
    last = None
    for attempt in range(max(1, retries)):
        tmp = None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # 临时名必须唯一：两个进程同时写同一个文件时，各写各的 .tmp，
            # 谁后 rename 谁算数，落地的总是完整的一份。
            # 用固定名（name + ".tmp"）时两边写进同一个文件，rename 上去的是两段交织的内容。
            fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, p)
            return True
        except OSError as exc:  # Windows：被 Defender / 编辑器占用 → EPERM / EBUSY / EACCES
            last = exc
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            time.sleep(0.05 * (attempt + 1))
    log.warning("json_write_failed path=%s err=%r", str(p), last)
    return False


def atomic_write_json(path, obj: Any, *, indent: int | None = None, sort_keys: bool = False,
                       retries: int = 4) -> bool:
    # 原子写 JSON（indent=None = 单行）。返回是否写成。
    # sort_keys 只给「同一份数据要按固定顺序落盘」的调用方（比如别名表，人要看 diff）。
    return atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=indent,
                                               sort_keys=sort_keys), retries=retries)


def read_json_checked(path, default: Any = None) -> tuple[Any, str]:
    # 读 JSON，并把三件事分开告诉调用方：读到了 / 本来就没有 / 坏了。
    # 为什么不合并成一句「默认值」：坏数据和「本来就没有」长得一模一样，调用方分不出来，
    # 就只能按默认值放宽——坏账本当没记过，于是再发一次（CA-057 的原型）。
    # 返回 (值, 状态)，状态 ∈ "ok" | "missing" | "corrupt"；
    # 读不出来（权限、被占用）算 corrupt 而不是 missing：不知道不等于没有。
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default, "missing"
    except OSError as exc:
        log.warning("json_read_failed path=%s err=%r default=%r", str(p), exc, default)
        return default, "corrupt"
    try:
        return json.loads(raw), "ok"
    except Exception as exc:  # noqa: BLE001  坏 JSON 一律回默认
        backup = None
        try:
            if not list(p.parent.glob(p.name + ".bad-*")):
                backup = p.with_name(p.name + ".bad-" + time.strftime("%Y%m%d-%H%M%S"))
                backup.write_text(raw, encoding="utf-8")
        except OSError:
            backup = None
        log.warning(
            "json_read_corrupt path=%s err=%r backup=%s default=%r",
            str(p), exc, str(backup) if backup else "skip", default,
        )
        return default, "corrupt"


def read_json(path, default: Any = None) -> Any:
    # 兼容口：只要值的调用方走这里（行为与改动前一致：缺失/坏 → 默认值 + 备份 + warning）。
    return read_json_checked(path, default)[0]
