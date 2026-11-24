# -*- coding: utf-8 -*-
"""裸旗标语义的锁：``X=`` 是「要开」，不是「没配」。"""
from __future__ import annotations

import importlib
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture()
def ef():
    from core.infra import envflags
    return importlib.reload(envflags)


def test_unset_uses_caller_default(ef, monkeypatch):
    monkeypatch.delenv("PROBE_X", raising=False)
    assert ef.flag("PROBE_X") is False
    assert ef.flag("PROBE_X", default=True) is True


def test_bare_flag_means_on(ef, monkeypatch):
    monkeypatch.setenv("PROBE_X", "")
    assert ef.flag("PROBE_X") is True
    monkeypatch.setenv("PROBE_Y", "   ")
    assert ef.flag("PROBE_Y") is True


def test_explicit_words(ef, monkeypatch):
    for word in ("1", "true", "yes", "on", "Y", "T"):
        monkeypatch.setenv("PROBE_X", word)
        assert ef.flag("PROBE_X") is True, word
    for word in ("0", "false", "no", "off", "N", "F"):
        monkeypatch.setenv("PROBE_X", word)
        assert ef.flag("PROBE_X") is False, word


def test_unknown_word_falls_back_to_default(ef, monkeypatch):
    monkeypatch.setenv("PROBE_X", "maybe")
    assert ef.flag("PROBE_X") is False
    assert ef.flag("PROBE_X", default=True) is True


def test_text_keeps_empty_as_default(ef, monkeypatch):
    monkeypatch.setenv("PROBE_T", "")
    assert ef.text("PROBE_T", "dflt") == "dflt"


@pytest.mark.parametrize("name", [
    "WX_DEBUG_DUMPS",
    "WX_INCREMENTAL_DECRYPT",
    "YINYUE_NO_REAP",
    "YINYUE_DESCRIBE_CHILD",
])
def test_no_bare_boolean_env_reads_left(name):
    # 模式由片段拼出来：本文件自己不该含有"旧写法"的字面量，否则锁会咬到锁自己。
    old = "os.environ.get(" + chr(34) + name + chr(34)
    skip = {".git", "__pycache__", ".venv", "venv", "data", "logs", "backups", "output", "test_tmp", ".test_tmp"}
    offenders = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = Path(root) / name
            text = path.read_text(encoding="utf-8", errors="replace")
            if old in text:
                offenders.append(str(path.relative_to(ROOT)).replace(os.sep, "/"))
    assert offenders == [], offenders

def _source_files(root):
    """仓库里的 .py 源文件；遍历时就剪掉 .test_tmp 等目录（rglob 会走进去，慢且会撞上历史临时文件）。"""
    skip = {"tests", ".test_tmp", ".venv", "venv", "node_modules", "__pycache__", ".git",
            "data", "logs", "backups", "output"}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if name.endswith(".py"):
                yield Path(base) / name

def test_no_blank_flag_name():
    """`flag("")` 恒等于 False，代码看着像在读开关、其实读了个不存在的名字。

    2026-10-29 抓到 4 处（chat_window / person_notes / memory_proposals / memory_sinking），
    都是批量迁移时把变量名漏掉的产物，测试只暴露了其中一处。
    """
    root = Path(__file__).resolve().parents[2]
    bad = []
    for path in _source_files(root):
        src = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\bflag\(\s*(?:\"\"|'')\s*\)", src):
            bad.append("%s:%d" % (path.relative_to(root).as_posix(), src[:m.start()].count("\n") + 1))
    assert not bad, bad
def test_text_is_imported_where_used():
    """已从 envflags 取 `flag` 的模块，若又调 `text(...)` 就必须一起 import `text`。

    `text()` 这名字在别处也常见（SQLAlchemy 的 `text()`），所以只看**已经**从
    `core.infra.envflags` 取名字的模块 —— 只盯这次批量迁移的那一类文件。
    """
    root = Path(__file__).resolve().parents[2]
    bad = []
    for path in _source_files(root):
        src = path.read_text(encoding="utf-8", errors="replace")
        hit = re.search(r"from core\.infra\.envflags import ([^\n]+)", src)
        if not hit:
            continue
        imported = hit.group(1)
        if re.search(r"\btext\b", imported):
            continue
        if re.search(r"(?<![\w.])text\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']", src):
            bad.append(path.relative_to(root).as_posix())
    assert not bad, bad
