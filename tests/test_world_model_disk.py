# -*- coding: utf-8 -*-
"""世界模型落盘可反查 + 跨会话读数包含盘上会话（2026-10-23，七件计划第 7 件）。

为什么要有这个守卫
==================
1. 盘上文件名是 ``sha1(chat_id)[:16]``——**反查不回来**。旧落盘只写事件数组，
   于是"盘上有 20 个会话"这件事在代码里等于不存在：`known_world_models()` 只返回
   本进程载入过的，重启后情绪读数凭空归零。
2. 旧形状（裸 list）必须**继续能读**（不能因为改格式把历史快照读成空），但
   **不许被枚举**：判不出是哪个会话，就不能猜。
3. `remember` 的 why 是"复查时唯一的线索"，缺了不拦。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：裸 ``TemporaryDirectory()`` /
# ``mkstemp()`` 在系统 TEMP 不可写时会静默回落到**仓库根**，落点就跑进工作区。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _tempdirs import make_db_dir  # noqa: E402

from core.context import situation_store as store  # noqa: E402
from core.context.situation import WorldEvent  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok   {name}")
    else:
        FAILS.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


class _Dir:
    def __init__(self) -> None:
        # 落点交给唯一入口（基座下的唯一子目录），不裸用系统 TEMP。
        self.path = make_db_dir("wmodel_")

    def __enter__(self):
        os.environ["YINYUE_WORLD_MODEL_DIR"] = str(self.path)
        store.reset_world_models()
        return self

    def __exit__(self, *exc):
        os.environ.pop("YINYUE_WORLD_MODEL_DIR", None)
        store.reset_world_models()
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def _seed(chat_id: str, *, level: float = 0.4, who_id: str = "") -> None:
    model = store.get_world_model(chat_id)
    payload = {"level": level, "label": "平静"}
    if who_id:
        payload["who_id"] = who_id
    model.events.append(
        WorldEvent(kind="emotion_observed", at="2026-10-23T12:00:00", payload=payload)
    )


def test_persist_records_chat_id() -> None:
    with _Dir() as d:
        _seed("chat-A")
        check("落盘成功", store.persist_world_model("chat-A"))
        files = list(d.path.glob("*.json"))
        check("写出一个文件", len(files) == 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        check("是 dict 形状", isinstance(payload, dict), str(type(payload)))
        check("里面写了 chat_id", payload.get("chat_id") == "chat-A")
        check("里面写了事件", isinstance(payload.get("events"), list) and payload["events"])
        store.reset_world_models()
        again = store.load_world_model("chat-A")
        check("读得回来", again is not None and len(again.events) == 1)


def test_legacy_list_still_loads() -> None:
    with _Dir() as d:
        path = store._model_path("chat-L")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([{"kind": "emotion_observed", "at": "2026-10-23T12:00:00",
                         "payload": {"level": 0.5}}]),
            encoding="utf-8",
        )
        loaded = store.load_world_model("chat-L")
        check("旧形状仍能按 chat_id 读", loaded is not None and len(loaded.events) == 1)
        check("但枚举不到（判不出是谁的）", "chat-L" not in store.world_models_on_disk())


def test_disk_enumeration() -> None:
    with _Dir() as d:
        _seed("chat-A")
        _seed("chat-B")
        store.persist_world_model("chat-A")
        store.persist_world_model("chat-B")
        (d.path / "broken.json").write_text("{not json", encoding="utf-8")
        store.reset_world_models()
        found = store.world_models_on_disk()
        check("枚举到两个会话", set(found) == {"chat-A", "chat-B"}, str(set(found)))
        check("坏文件被跳过", len(found) == 2)
        check("事件带回来了", any(m.events for m in found.values()))


def test_all_world_models_prefers_process() -> None:
    with _Dir() as d:
        _seed("chat-A", level=0.4)
        store.persist_world_model("chat-A")
        store.reset_world_models()
        store.get_world_model("chat-A")  # 进程内重新载入
        store.get_world_model("chat-A").events.append(
            WorldEvent(kind="emotion_observed", at="2026-10-23T12:05:00",
                       payload={"level": 0.2, "label": "平静"})
        )
        before = len(store.known_world_models()["chat-A"].events)
        merged = store.all_world_models()
        check("进程内优先", len(merged["chat-A"].events) == 2, str(len(merged["chat-A"].events)))
        # 2026-10-23 真 bug（本守卫抓到）：枚举如果走 restore_world_model，会把
        # _MODELS[key] 换成盘上的旧快照——"看一眼盘上有哪些会话"就把活模型回滚了。
        check(
            "枚举不许回滚进程内模型",
            len(store.known_world_models()["chat-A"].events) == before,
            str(len(store.known_world_models()["chat-A"].events)),
        )
        _ = WorldEvent


def test_emotion_reading_includes_disk_sessions() -> None:
    from core.context.emotion import attitude_across_chats, mood_across_chats

    with _Dir() as d:
        for chat in ("chat-A", "chat-B"):
            _seed(chat, who_id="p1")
        store.persist_world_model("chat-A")
        store.persist_world_model("chat-B")
        store.reset_world_models()  # 模拟重启：进程里一个都没有
        check("重启后进程内为空", store.known_world_models() == {})
        mood = mood_across_chats(now=None)
        check("盘上两个会话都算进来", mood.samples == 2, str(mood.samples))
        att = attitude_across_chats("p1")
        check("态度也跨会话", att.samples == 2, str(att.samples))


def test_remember_has_why() -> None:
    from agents.tools.memory import RememberTool

    props = RememberTool.parameters
    check("remember 有 why", "why" in props)
    check("why 不是必填", not props["why"].get("required", False))
    src = (ROOT / "agents/tools/memory.py").read_text(encoding="utf-8")
    check("why 进了 metadata", '"why": why' in src)
    check("why 截断 200", '[:200]' in src)


def main() -> int:
    for fn in (
        test_persist_records_chat_id,
        test_legacy_list_still_loads,
        test_disk_enumeration,
        test_all_world_models_prefers_process,
        test_emotion_reading_includes_disk_sessions,
        test_remember_has_why,
    ):
        print(fn.__name__)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            FAILS.append("%s raised %r" % (fn.__name__, exc))
    print("RESULT %d failed / %d checks" % (len(FAILS), COUNT))
    for item in FAILS:
        print("  - " + item)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())