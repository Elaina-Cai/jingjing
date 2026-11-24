# -*- coding: utf-8 -*-
"""批D Step D1：⑩ 数据通道统一（状态落盘/恢复、检索、人设读取）的接线测试。

口径：D1 是**纯代理迁移**（无新 flag、无行为变化）。因此测试分两层：
* 行为层：通道函数与底层函数逐参对齐、异常原样穿透、状态/检索/人设语义不变；
* 结构层：迁移过的调用点不再直连 owner 模块，全部经 ``core.execution.data_channels``；
  通道模块自身保持纯（顶层不 import core.*）。
"""
from __future__ import annotations

import ast
import os
import pathlib
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _tempdirs import make_db_dir  # noqa: E402
from core.context.situation import WorldModel, observe_message  # noqa: E402
import core.context.situation_store as situation_store  # noqa: E402
import core.execution.data_channels as data_channels  # noqa: E402
import core.execution.persona_channel as persona_channel  # noqa: E402
import core.memory.recall_router as recall_router  # noqa: E402

#: D1 迁移过的调用点：不许再直连底层 owner，只许经通道。
MIGRATED = (
    "core/affect/bridge.py",
    "core/context/assembler.py",
    "core/context/perception.py",
    "core/context/intention_bridge.py",
    "core/context/intention_tick.py",
    "agents/memory.py",
    "agents/tools/memory.py",
    "agents/tools/search.py",
    "core/routing/turn_pipeline.py",
)

OWNER_MARKERS = (
    "core.context.situation_store",
    "core.memory.recall_router",
    "core.execution.persona_channel",
)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class _env:
    def __init__(self, **kv):
        self.kv = kv

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kv}
        for k, v in self.kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        return self

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def test_surface_and_digest():
    assert set(data_channels.CHANNELS) == {"state", "retrieval", "persona"}
    for name, entries in data_channels.CHANNELS.items():
        assert entries, name
        for entry in entries:
            assert entry in data_channels.__all__, entry
            assert callable(getattr(data_channels, entry)), entry
    digest = data_channels.channel_digest()
    assert digest["entries"] == 12, digest
    assert digest["channels"]["state"]["owner"] == "core.context.situation_store"
    assert digest["channels"]["retrieval"]["owner"] == "core.memory.recall_router"
    assert digest["channels"]["persona"]["owner"] == "core.execution.persona_channel"


def test_state_roundtrip_with_dir():
    # 临时目录唯一入口（tests/_tempdirs.py）：裸 TemporaryDirectory() 会回落进仓库根，
    # 被 tests/test_media_cache_and_mime.py::test_temp_entry_is_the_single_entry 拦住。
    tmp = make_db_dir("d1_world_")
    try:
        with _env(YINYUE_WORLD_MODEL_DIR=str(tmp)):
            situation_store.reset_world_models()
            try:
                model = data_channels.get_world("d1-c1")
                observe_message(model, who="A", text="hi", at="2026-10-14 09:00")
                assert data_channels.save_world("d1-c1") is True
                assert list(tmp.glob("*.json"))
                situation_store.reset_world_models()
                assert data_channels.known_worlds() == {}
                loaded = data_channels.load_world("d1-c1")
                assert loaded is not None
                assert [e.kind for e in loaded.events] == ["message_observed"]
                snap = data_channels.snapshot_world("d1-c1")
                assert snap and snap[0]["kind"] == "message_observed"
            finally:
                situation_store.reset_world_models()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_state_without_dir_is_memory_only():
    with _env(YINYUE_WORLD_MODEL_DIR=None):
        situation_store.reset_world_models()
        try:
            assert data_channels.save_world("d1-nodir") is False
            assert data_channels.load_world("d1-nodir") is None
            model = data_channels.get_world("d1-nodir")
            assert isinstance(model, WorldModel)
            assert "d1-nodir" in data_channels.known_worlds()
        finally:
            situation_store.reset_world_models()


def test_state_errors_pass_through():
    def boom(*args, **kwargs):
        raise RuntimeError("store down")

    with mock.patch.object(situation_store, "persist_world_model", boom):
        try:
            data_channels.save_world("d1-boom")
        except RuntimeError as exc:
            assert "store down" in str(exc)
        else:
            raise AssertionError("save_world 吞掉了底层异常")
    with mock.patch.object(situation_store, "get_world_model", boom):
        try:
            data_channels.get_world("d1-boom")
        except RuntimeError:
            pass
        else:
            raise AssertionError("get_world 吞掉了底层异常")


def test_retrieval_argument_passthrough():
    calls = []

    def fake_items(context, query="", *, kinds=None, limit=8, hub=None):
        calls.append(("items", context, query, kinds, limit, hub))
        return ["item"]

    def fake_recall(context, query="", *, limit=5, gateway=None):
        calls.append(("recall", context, query, limit, gateway))
        return ["hit"]

    def fake_render(hits, *, header="", max_chars=1200):
        calls.append(("render", tuple(hits), header, max_chars))
        return "rendered"

    ctx = SimpleNamespace(name="ctx")
    with mock.patch.object(recall_router, "recall_items_for_context", fake_items):
        with mock.patch.object(recall_router, "recall_for_context", fake_recall):
            with mock.patch.object(recall_router, "render_hits", fake_render):
                assert data_channels.recall_items_for_context(
                    ctx, "q", kinds=["personal"], limit=3, hub="H"
                ) == ["item"]
                assert data_channels.recall_for_context(ctx, "q", limit=2, gateway="G") == ["hit"]
                assert data_channels.render_hits(["a"], header="[H]", max_chars=99) == "rendered"
    assert calls == [
        ("items", ctx, "q", ["personal"], 3, "H"),
        ("recall", ctx, "q", 2, "G"),
        ("render", ("a",), "[H]", 99),
    ], calls


def test_retrieval_missing_principal_and_errors():
    assert data_channels.recall_items_for_context(None, "q") == []
    assert data_channels.recall_for_context(None, "q") == []
    assert isinstance(data_channels.render_hits([]), str)

    def boom(*args, **kwargs):
        raise ValueError("router down")

    with mock.patch.object(recall_router, "recall_items_for_context", boom):
        try:
            data_channels.recall_items_for_context(SimpleNamespace(), "q")
        except ValueError:
            pass
        else:
            raise AssertionError("检索异常被吞")


def test_persona_argument_passthrough():
    calls = []

    def fake_read(repo=None, *, persona_id=None):
        calls.append(("read", repo, persona_id))
        return "VIEW"

    def fake_note(view, *, limit=80):
        calls.append(("note", view, limit))
        return "[人设]"

    def fake_digest(view, *, limit=80):
        calls.append(("digest", view, limit))
        return {"has_system_prompt": False}

    with mock.patch.object(persona_channel, "read_persona", fake_read):
        with mock.patch.object(persona_channel, "persona_note", fake_note):
            with mock.patch.object(persona_channel, "persona_digest", fake_digest):
                with mock.patch.object(persona_channel, "persona_channel_mode", lambda: "on"):
                    assert data_channels.read_persona("R", persona_id=7) == "VIEW"
                    assert data_channels.persona_note("V", limit=11) == "[人设]"
                    assert data_channels.persona_digest("V", limit=12) == {"has_system_prompt": False}
                    assert data_channels.persona_channel_mode() == "on"
    assert calls == [("read", "R", 7), ("note", "V", 11), ("digest", "V", 12)], calls


def test_migrated_call_sites_route_through_channel():
    for rel in MIGRATED:
        text = _read(rel)
        assert "core.execution.data_channels" in text, rel
        for marker in OWNER_MARKERS:
            assert marker not in text, f"{rel} 仍直连 {marker}"


def test_channel_module_is_pure_proxy():
    tree = ast.parse(_read("core/execution/data_channels.py"))
    top = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            top.append(node.module or "")
        elif isinstance(node, ast.Import):
            top.extend(alias.name for alias in node.names)
    assert top == ["__future__", "typing"], top
    # 所有 core.* 依赖必须写在函数体内（延迟 import），模块导入零副作用。
    assert not any(name.startswith("core") for name in top)


def test_call_sites_still_compile():
    files = [str(ROOT / rel) for rel in MIGRATED]
    py = str((ROOT / ".venv_it" / "Scripts" / "python.exe"))
    proc = subprocess.run([py, "-m", "py_compile", *files], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-500:]


def main() -> bool:
    checks = [
        test_surface_and_digest,
        test_state_roundtrip_with_dir,
        test_state_without_dir_is_memory_only,
        test_state_errors_pass_through,
        test_retrieval_argument_passthrough,
        test_retrieval_missing_principal_and_errors,
        test_persona_argument_passthrough,
        test_migrated_call_sites_route_through_channel,
        test_channel_module_is_pure_proxy,
        test_call_sites_still_compile,
    ]
    failed = []
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{check.__name__}: {exc!r}")
    if failed:
        print(f"data-channel checks: {len(checks) - len(failed)}/{len(checks)} passed")
        for item in failed:
            print("FAIL:", item)
        return False
    print(f"data-channel checks: {len(checks)}/{len(checks)} passed")
    return True


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
