# -*- coding: utf-8 -*-
"""C：方法经验进入首轮 prompt，并按作用域/能力/相关性收敛。

只测「读侧接线」：经验怎么写由 ``test_experience_commit`` 负责；
这里证明 run_loop 真的会带、带的是有效版本、条件不成立时不乱套用。
"""
from __future__ import annotations

import sqlite3

import pytest

from core.context import cognitive_commit as cc
from core.context import reflection as rf
from test_appraisal_prompt_integration import Model, drive, runtime  # noqa: F401 - fixture


@pytest.fixture()
def lessons_db(runtime):
    brain, tmp = runtime
    conn = cc.connect(str(tmp / "cognitive.db"))
    cc.ensure_schema(conn)
    yield brain, tmp, conn
    conn.close()


def _record(conn, *, method="search_local_docs", outcome="success", lesson="找项目资料时先查文档索引里的路径",
            confidence=0.9, scope=cc.Scope("wechat", "acct", "chat-1"), evidence=("e1",)):
    result = rf.record_lesson(conn, scope=scope, method=method, outcome=outcome,
                              evidence_refs=evidence, lesson=lesson, confidence=confidence)
    assert result.committed, result


def _ask(brain, tmp, *, query="帮我找项目里的那份资料", chat="chat-1"):
    model = Model([{"content": "", "tool_calls": []}])
    drive(brain.run_loop(str(tmp / "platforms/wechat/acct/account.db"), "世界", llm=model,
                         dry_run=True, rehearse=True, max_steps=1, platform="wechat",
                         chat_id=chat, sender_id="person-a", recall_query=query))
    return model.prompts[0]


def test_lesson_reaches_first_prompt(lessons_db, monkeypatch):
    brain, tmp, conn = lessons_db
    monkeypatch.setattr("core.tools.catalog.read_only_in_plan", lambda: {"search_local_docs", "look"})
    _record(conn)
    prompt = _ask(brain, tmp)
    joined = prompt[0]["content"] + "\n" + prompt[1]["content"]
    assert "用过的办法" in joined
    assert "找项目资料时先查文档索引里的路径" in joined
    assert "不替代当前证据" in joined


def test_lesson_is_scoped_to_its_chat(lessons_db, monkeypatch):
    brain, tmp, conn = lessons_db
    monkeypatch.setattr("core.tools.catalog.read_only_in_plan", lambda: {"search_local_docs"})
    _record(conn)
    prompt = _ask(brain, tmp, chat="another-chat")
    joined = prompt[0]["content"] + "\n" + prompt[1]["content"]
    assert "找项目资料时先查文档索引里的路径" not in joined


def test_unavailable_method_is_not_taught(lessons_db, monkeypatch):
    brain, tmp, conn = lessons_db
    monkeypatch.setattr("core.tools.catalog.read_only_in_plan", lambda: {"look"})
    _record(conn)
    prompt = _ask(brain, tmp)
    joined = prompt[0]["content"] + "\n" + prompt[1]["content"]
    assert "用过的办法" not in joined


def test_unrelated_query_gets_background_not_rule(lessons_db, monkeypatch):
    brain, tmp, conn = lessons_db
    monkeypatch.setattr("core.tools.catalog.read_only_in_plan", lambda: {"search_local_docs"})
    _record(conn)
    prompt = _ask(brain, tmp, query="今晚吃什么")
    joined = prompt[0]["content"] + "\n" + prompt[1]["content"]
    assert "只作背景" in joined
    assert "不一定适用于眼前这件事" in joined


def test_new_evidence_supersedes_old_lesson(lessons_db, monkeypatch):
    brain, tmp, conn = lessons_db
    monkeypatch.setattr("core.tools.catalog.read_only_in_plan", lambda: {"search_local_docs"})
    _record(conn)
    current = rf.current_lessons_for_scope(cc.Scope("wechat", "acct", "chat-1"))[0]
    result = rf.record_lesson(conn, scope=cc.Scope("wechat", "acct", "chat-1"),
                              method="search_local_docs", outcome="failure",
                              evidence_refs=("e2",), lesson="索引过期了，先看目录时间再引用",
                              confidence=0.8, supersedes=current.event_id)
    assert result.committed, result
    prompt = _ask(brain, tmp)
    joined = prompt[0]["content"] + "\n" + prompt[1]["content"]
    assert "索引过期了" in joined
    assert "找项目资料时先查文档索引里的路径" not in joined
