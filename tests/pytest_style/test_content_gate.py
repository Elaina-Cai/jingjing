# -*- coding: utf-8 -*-
"""内容闸（2026-11-04，用户 m03054 第 3 项：全 C 之后这四类不许自动发）。

每个用例都是**行为断言**：喂一句话，问它能不能自动发出去。
真机回放（3322 条她发过的消息）的拦截率与已知误拦见文档 §25。
"""

from __future__ import annotations

import asyncio

import pytest

from db import world_db
from loop import content_gate as gate
from loop import outbound_gate, ports
from loop import speak_policy


def test_an_ordinary_line_goes_through():
    assert gate.check("下午三点倒垃圾") == (True, "ok")
    assert gate.check("今天下雨记得带伞") == (True, "ok")


def test_an_amount_of_money_is_blocked():
    assert gate.check("门票 128 元，我先垫了") == (False, "content:money")
    assert gate.check("这个 39.9 块钱") == (False, "content:money")


def test_the_yuan_sign_counts_as_money():
    assert gate.check("一共 ¥88") == (False, "content:money")


def test_a_payment_action_counts_as_money():
    assert gate.check("我转账给你了") == (False, "content:money")
    assert gate.check("你扫这个二维码就行") == (False, "content:money")


def test_a_long_number_looks_like_a_card():
    assert gate.check("卡号 6222021234567890123") == (False, "content:money")


def test_a_phone_number_is_blocked():
    assert gate.check("我手机 13812345678，有事打我电话") == (False, "content:address")


def test_a_real_address_is_blocked_even_with_spaces():
    assert gate.check("地址是西湖区文一西路 100 号 3 号楼 502 室") == (False, "content:address")
    assert gate.check("3 号楼 502 室") == (False, "content:address")


def test_an_email_is_blocked():
    assert gate.check("发到 whale@example.com") == (False, "content:address")


def test_answering_for_someone_else_is_blocked():
    assert gate.check("我替他答应了，周五之前给") == (False, "content:others")
    assert gate.check("我帮你答应下来了") == (False, "content:others")


def test_a_time_commitment_is_blocked():
    assert gate.check("我保证明天送到") == (False, "content:promise")
    assert gate.check("包在我身上，明早八点叫你们") == (False, "content:promise")


def test_a_promise_without_a_time_is_still_allowed():
    assert gate.check("我保证用好") == (True, "ok")


def test_a_question_is_not_a_commitment():
    assert gate.check("要不要我垫？") == (True, "ok")
    assert gate.check("需要我帮你报名吗？") == (True, "ok")


def test_refusing_to_do_it_is_not_a_commitment():
    assert gate.check("转账这事我办不了，手里没有能动你钱的口子") == (True, "ok")
    assert gate.check("下单要花钱，那也是主人的钱，我不能拿") == (True, "ok")


def test_a_system_message_is_not_someone_talking():
    assert gate.check('<sysmsg type="revokemsg"><revokemsg /></sysmsg>') == (True, "ok")


def test_empty_text_is_not_a_content_block():
    assert gate.check("") == (True, "ok")
    assert gate.check(None) == (True, "ok")


def test_the_explanation_is_for_humans():
    assert "钱" in gate.explain("content:money")
    assert "地址" in gate.explain("content:address")
    assert gate.explain("duplicate") == "duplicate"


def test_the_outbound_gate_asks_the_content_gate_first():
    outbound_gate.reset()
    ok, why = outbound_gate.check("c1", "门票 128 元，我先垫了")
    assert (ok, why) == (False, "content:money")
    # 被内容闸拦下来的一律不占窗口：同一句后来改成不带钱的说法能发。
    assert outbound_gate.check("c1", "我先垫了") == (True, "ok")


def test_a_blocked_line_does_not_occupy_the_dedupe_window():
    outbound_gate.reset()
    assert outbound_gate.check("c1", "我手机 13812345678")[0] is False
    assert outbound_gate.check("c1", "我在的")[0] is True


class _Res:
    def __init__(self, ok=True, data=None):
        self.ok = ok
        self.data = data or {}


def test_say_reports_a_content_skip_instead_of_sending(monkeypatch):
    sent = []

    def _enqueue(group_id, chat_id, items, **kw):
        sent.append((chat_id, [str(i) for i in items]))
        return list(range(1, len(items) + 1))

    monkeypatch.setattr(world_db, "enqueue", _enqueue)
    monkeypatch.setattr(ports, "resolve_chat_id", lambda c: c)
    monkeypatch.setattr(speak_policy, "allowed", lambda *a, **k: (True, "ok"))
    outbound_gate.reset()

    res = asyncio.run(ports.say("我转账给你了，一共 88 元", dry_run=False, chat_id="c1"))
    assert sent == []
    assert res.ok is True, "被拦是裁决，不是失败"
    assert res.data["ack"] == "skipped"
    assert res.data["why"] == "content:money"


def test_a_normal_line_still_goes_out(monkeypatch):
    sent = []

    def _enqueue(group_id, chat_id, items, **kw):
        sent.append((chat_id, [str(i) for i in items]))
        return list(range(1, len(items) + 1))

    monkeypatch.setattr(world_db, "enqueue", _enqueue)
    monkeypatch.setattr(ports, "resolve_chat_id", lambda c: c)
    monkeypatch.setattr(speak_policy, "allowed", lambda *a, **k: (True, "ok"))
    outbound_gate.reset()

    asyncio.run(ports.say("我去问一下场地", dry_run=False, chat_id="c1"))
    assert len(sent) == 1
    assert sent[0][0] == "c1"
    assert "我去问一下场地" in sent[0][1][0]
