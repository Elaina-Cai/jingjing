"""证据编号解析失败不能是静默的。

线上实况（2026-11-21 核对）：`inbound_event` 28778 行，`platform_msg_id` 全空。
`_evidence_ref` 于是拿本地行号 `wechat:<acct>:<account.db id>` 去事件流里按
平台消息号回查，永远查不到 → 这一拍 `visible_event_ids` 全空 → 评价工具
`evaluate_experience` 从未出现在工具表里（重启前本进程 27 拍里 0 次）、
认知库里一条 appraisal 都没有。整个过程没有任何日志。

这里锁两件事：解析成功就不吵；有真人说话却一条都没解析出来，必须留痕且限流。
"""

import sys
from types import SimpleNamespace

import pytest

from loop import run as loop_run


def _row(mid, *, self_=False, platform_msg_id=""):
    return SimpleNamespace(msg_id=mid, platform_msg_id=platform_msg_id, is_self=self_)


def _scope(chat="10000000001@chatroom"):
    return SimpleNamespace(platform="wechat", account_id="acct", chat_id=chat)


@pytest.fixture(autouse=True)
def _fresh_guard():
    from core.infra import warn_guard

    warn_guard.reset()
    yield
    warn_guard.reset()


def test_self_rows_are_not_sources(monkeypatch):
    monkeypatch.setattr(loop_run.intention_close, "_evidence_ref", lambda row, scope: "evt:" + str(row.msg_id))
    pairs = loop_run._fresh_message_sources([_row(1), _row(2, self_=True)], _scope())
    assert [ref for _, ref in pairs] == ["evt:1"]


def test_resolved_refs_are_quiet(monkeypatch):
    seen = []
    monkeypatch.setattr(loop_run.intention_close, "_evidence_ref", lambda row, scope: "evt:" + str(row.msg_id))
    monkeypatch.setattr(loop_run, "warn", lambda *a, **k: seen.append((a, k)))
    loop_run._fresh_message_sources([_row(1)], _scope())
    assert seen == []


def test_all_refs_missing_is_reported_once(monkeypatch):
    seen = []
    monkeypatch.setattr(loop_run.intention_close, "_evidence_ref", lambda row, scope: "")
    monkeypatch.setattr(loop_run, "warn", lambda *a, **k: seen.append((a, k)))
    rows = [_row(1), _row(2)]
    loop_run._fresh_message_sources(rows, _scope())
    loop_run._fresh_message_sources(rows, _scope())
    assert len(seen) == 1, seen
    args, kwargs = seen[0]
    assert args[0] == "evidence_refs_unresolved"
    assert kwargs["rows"] == 2
    assert kwargs["platform_msg_id_empty"] == 2


def test_one_resolved_ref_silences_the_warning(monkeypatch):
    seen = []
    monkeypatch.setattr(loop_run.intention_close, "_evidence_ref",
                        lambda row, scope: "evt:1" if row.msg_id == 1 else "")
    monkeypatch.setattr(loop_run, "warn", lambda *a, **k: seen.append((a, k)))
    loop_run._fresh_message_sources([_row(1), _row(2)], _scope())
    assert seen == []


def test_evidence_ref_prefers_the_platform_message_id(monkeypatch):
    """带平台消息号的行走平台号；空串说明采集层没回填（本次线上事故形状）。"""
    from core.context import intention_close

    class _Store:
        def get_by_external_msg_id(self, *, platform, bot_account_id, chat_id, external_msg_id):
            return SimpleNamespace(event_id="evt:ok") if external_msg_id == "2929938966730799305" else None

    class _Shadow:
        @staticmethod
        def get_default_store():
            return _Store()

    # 2026-11-24：原来直接替换 sys.modules 再 pop 掉，真模块会被下一个 import 重新加载成
    # 新对象——之后所有「patch 了 shadow 再断言」的用例都会拿到未打补丁的新模块
    # （tests/pytest_style 全套里 39 条 test_experience_commit 失败就是这个原因）。
    # 交给 monkeypatch 恢复原模块。
    monkeypatch.setitem(sys.modules, "core.terminal.shadow", _Shadow)
    scope = _scope()
    good = SimpleNamespace(platform_msg_id="2929938966730799305", msg_id=4584638694372947175)
    local_only = SimpleNamespace(platform_msg_id="", msg_id=4584638694372947175)
    assert intention_close._evidence_ref(good, scope) == "evt:ok"
    assert intention_close._evidence_ref(local_only, scope) == ""

