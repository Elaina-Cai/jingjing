# -*- coding: utf-8 -*-
"""`read_file` 的边界：能读文档，读不到别的东西。

这个工具是给她的**第一双手**（之前只能回「我翻不了项目里的文件」）。所以这里
重点不是「能读」，而是「只读到该读的地方」：越界、私密、二进制、超大，一律拒且给原因。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agents.tools.local_files import ReadFileTool


def _read(tool: ReadFileTool, path: str, **kwargs):
    return asyncio.run(tool.execute(path=path, **kwargs))


def _tree(tmp_path: Path) -> ReadFileTool:
    (tmp_path / "docs").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "backups").mkdir()
    (tmp_path / "docs" / "README.md").write_text("# 索引\n- 部署说明：docs/deploy.md\n", encoding="utf-8")
    (tmp_path / "output" / "报告.md").write_text("结论先行", encoding="utf-8")
    (tmp_path / "README.md").write_text("根目录说明", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("规矩", encoding="utf-8")
    (tmp_path / "data" / "x.db").write_bytes(b"SQLite format 3\x00")
    (tmp_path / "backups" / "old.md").write_text("旧备份", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=secret", encoding="utf-8")
    (tmp_path / "docs" / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    (tmp_path / "docs" / "big.md").write_text("字" * 200000, encoding="utf-8")
    (tmp_path / "docs" / "mid.md").write_text("字" * 1200, encoding="utf-8")
    (tmp_path / "scripts.py").write_text("print(1)", encoding="utf-8")
    return ReadFileTool(root=tmp_path)


def test_reads_docs_and_output(tmp_path):
    tool = _tree(tmp_path)
    got = _read(tool, "docs/README.md")
    assert not got.is_error and "docs/deploy.md" in got.output
    assert got.data["path"] == "docs/README.md"
    assert not _read(tool, "output/报告.md").is_error
    # 根目录的 *.md 也放行（AGENTS.md / README.md 属于文档）
    assert not _read(tool, "README.md").is_error


def test_refuses_outside_the_two_roots(tmp_path):
    tool = _tree(tmp_path)
    for path, hint in (("data/x.db", "不给读"), ("backups/old.md", "不给读"),
                       ("Data/secret.md", "不给读"), ("docs/Backups/x.md", "不给读"),
                       ("scripts.py", "只能读"), ("docs/../../etc/passwd", "..")):
        got = _read(tool, path)
        assert got.is_error, path
        assert hint in got.error, (path, got.error)


def test_refuses_secrets_and_absolute_paths(tmp_path):
    tool = _tree(tmp_path)
    assert "不给读" in _read(tool, ".env").error
    assert "绝对路径" in _read(tool, str(tmp_path / "docs" / "README.md")).error
    assert "绝对路径" in _read(tool, "C:/Windows/win.ini").error


def test_refuses_binary_and_oversize(tmp_path):
    tool = _tree(tmp_path)
    assert "二进制" in _read(tool, "docs/blob.bin").error
    assert "太大" in _read(tool, "docs/big.md").error


def test_truncates_long_text_but_says_so(tmp_path):
    tool = _tree(tmp_path)
    got = _read(tool, "docs/mid.md", max_chars=200)
    assert got.data["truncated"] is True
    assert got.data["chars"] == 1200
    assert "只给前" in got.output


def test_missing_file_is_an_error_not_an_empty_success(tmp_path):
    tool = _tree(tmp_path)
    got = _read(tool, "docs/nope.md")
    assert got.is_error and "没有这个文件" in got.error


def test_tool_is_declared_and_read_only():
    """实现了却没声明 = 模型看不见；声明了却没进只读清单 = 计划里调不动。"""
    from core.tools.catalog import read_only_in_plan
    from core.tools.declared import DECLARED

    names = {name for name, _effect, _purpose in DECLARED}
    assert "read_file" in names
    assert "read_file" in read_only_in_plan()


def test_runtime_gate_allows_project_file_reads_only_for_the_owner(monkeypatch):
    from types import SimpleNamespace

    from core import bootstrap
    from core.terminal import authority
    from loop import ports

    executed = []

    class Tool:
        async def execute(self, **kwargs):
            executed.append(kwargs)
            return SimpleNamespace(output="正文", error="")

    tool = Tool()
    monkeypatch.setattr(bootstrap, "init_singletons", lambda **kwargs: None)
    monkeypatch.setattr(bootstrap, "get_tools", lambda: {"read_file": tool})
    token = ports._READ_SCOPE.set(ports.ReadScope("wechat", "acct", "room@chatroom"))
    try:
        monkeypatch.setattr(
            authority, "active_context_principal",
            lambda ctx: SimpleNamespace(is_owner=False),
        )
        denied = asyncio.run(ports.act("read_file", {"path": "docs/a.md"}))
        assert not denied.ok and "只允许主人" in denied.error
        assert executed == []

        monkeypatch.setattr(
            authority, "active_context_principal",
            lambda ctx: SimpleNamespace(is_owner=True),
        )
        allowed = asyncio.run(ports.act("read_file", {"path": "docs/a.md"}))
        assert allowed.ok and executed == [{"path": "docs/a.md"}]
    finally:
        ports._READ_SCOPE.reset(token)
