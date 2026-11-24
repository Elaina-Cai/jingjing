"""重启恢复：本地兜底留下的执行关闭，下次启动补写进认知库。

只导入「有执行证据」的关闭；过期/观察类关闭没有执行事实，不冒充完成。
导入必须幂等：重复启动不会多出第二条 close，也不会重复加标记。
"""
import pytest

from core.context import situation
from core.context.cognitive_commit import (
    Scope,
    connect,
    invalidate_closed_cache,
    read_state,
)
from core.context.goal_close import close_local
from core.context.goal_reconcile import (
    import_all_pending,
    import_pending_closes,
    pending_local_closes,
)
from core.context.situation_store import (
    get_world_model,
    persist_world_model,
    reset_world_models,
)

KEY = "提醒:喝水"
AT = "2026-11-07 10:01"
SCOPE = Scope("wechat", "bot", "c1", "private", "")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch, request):
    monkeypatch.setenv("YINYUE_COGNITIVE_DB", str(tmp_path / "cognitive.db"))
    monkeypatch.setenv("YINYUE_WORLD_MODEL_DIR", str(tmp_path / "world"))
    from types import SimpleNamespace
    from core.terminal import store_port
    status = "failed" if "failure_is_abandoned" in request.node.name else "done"
    row = SimpleNamespace(source_platform="wechat", source_account_id="bot", source_chat_id="c1", status=status)
    monkeypatch.setattr(store_port, "get_repo", lambda: SimpleNamespace(reminders=SimpleNamespace(get=lambda key: row if key == 1 else None)))
    invalidate_closed_cache()
    reset_world_models()
    yield
    reset_world_models()
    invalidate_closed_cache()


def _model(scope=True):
    return situation.WorldModel(
        scope=(
            {"platform": "wechat", "account_id": "bot", "chat_id": "c1", "chat_type": "private"}
            if scope
            else {}
        )
    )


def test_local_fallback_keeps_the_execution_evidence():
    model = _model()
    situation.open_intention(model, KEY, kind="remind", due_at="2026-11-07 10:00")

    close_local(model, KEY, at=AT, result="sent", evidence_ref="reminder:1")

    assert pending_local_closes(model) == (
        {"key": KEY, "at": AT, "result": "sent", "evidence_ref": "reminder:1", "state": ""},
    )


def test_local_close_without_evidence_is_not_imported():
    model = _model()
    model.add("intention_closed", {"key": "过期的事", "result": "expired"})

    assert pending_local_closes(model) == ()


def test_import_writes_the_close_and_marks_it_once():
    model = _model()
    situation.open_intention(model, KEY, kind="remind", due_at="2026-11-07 10:00")
    close_local(model, KEY, at=AT, result="sent", evidence_ref="reminder:1")

    assert import_pending_closes("c1", model) == 1
    conn = connect()
    try:
        state = read_state(conn, KEY)
    finally:
        conn.close()
    assert state["operation"] == "close"
    assert state["payload"]["result"] == "sent"
    assert pending_local_closes(model) == (), "导入成功要留标记，避免下次重复"
    assert import_pending_closes("c1", model) == 0


def test_import_skips_models_without_scope():
    model = _model(scope=False)
    situation.open_intention(model, KEY, kind="remind", due_at="2026-11-07 10:00")
    # 存量文件可能已带有无坐标的 pending 记录；导入时仍须拒绝。
    model.add("intention_closed", {"key": KEY, "result": "sent", "evidence_ref": "reminder:1", "_pending": True}, at=AT)

    assert import_pending_closes("c1", model) == 0
    conn = connect()
    try:
        assert read_state(conn, KEY) is None
    finally:
        conn.close()


def test_startup_scan_imports_from_disk_models():
    model = get_world_model("c1", scope=SCOPE)
    situation.open_intention(model, KEY, kind="remind", due_at="2026-11-07 10:00")
    close_local(model, KEY, at=AT, result="sent", evidence_ref="reminder:1")
    assert persist_world_model("c1")

    reset_world_models()                     # 模拟重启：内存全丢

    report = import_all_pending()

    assert report.get("c1") == 1
    conn = connect()
    try:
        assert read_state(conn, KEY)["operation"] == "close"
    finally:
        conn.close()
