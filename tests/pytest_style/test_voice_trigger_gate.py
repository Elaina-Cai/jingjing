# -*- coding: utf-8 -*-

"""BR-062：语音触发的前置条件（只有判据，没有发送）。"""

from __future__ import annotations



import glob

import os



import pytest



from core.rules import voice_gate as vg



ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NOW = 1_800_000_000.0





def _allow(**kw):

    base = dict(chat_kind='group', who='owner', owner_spoke_at=NOW - 60,

                now=NOW, owner_key='c1|m1', used=())

    base.update(kw)

    return vg.allow(**base)





def test_private_owner_allowed():

    assert _allow(chat_kind='private') == (True, 'private_owner')





def test_private_stranger_blocked():

    assert _allow(chat_kind='private', who='other') == (False, 'not_owner_private')





def test_allow_anyone_overrides_everything():

    for kind in ('private', 'group', ''):

        assert vg.allow(chat_kind=kind, who='other', allow_anyone=True) == (True, 'anyone_allowed')





def test_group_owner_silent():

    assert _allow(owner_spoke_at=None) == (False, 'owner_silent')





def test_window_is_ten_minutes_and_edges():

    assert _allow(owner_spoke_at=NOW - (vg.WINDOW_S - 1))[0] is True

    assert _allow(owner_spoke_at=NOW - (vg.WINDOW_S + 1)) == (False, 'owner_stale')





def test_future_clock_blocked():

    assert _allow(owner_spoke_at=NOW + 3600)[0] is False





def test_group_needs_anchor_msg():

    assert _allow(owner_key='') == (False, 'no_anchor_msg')





def test_group_msg_already_used():

    assert _allow(used={'c1|m1'}) == (False, 'already_replied_to_this_msg')





def test_unknown_kind_fails_closed():

    assert _allow(chat_kind='') == (False, 'unknown_chat_kind')





def test_spoke_at_from_text():

    assert vg.spoke_at_from_text('2026-06-11 12:49:40.000000') is not None

    assert vg.spoke_at_from_text('2026-06-11 12:49:40') is not None

    assert vg.spoke_at_from_text('1800000000') == 1_800_000_000.0

    assert vg.spoke_at_from_text('不是时间') is None

    assert vg.spoke_at_from_text('') is None





@pytest.fixture()

def book(tmp_path, monkeypatch):

    monkeypatch.setattr(vg, 'MARKER', str(tmp_path / 'voice_trigger.json'))

    vg.reset()

    yield vg

    vg.reset()





def test_mark_used_is_idempotent_and_persists(book):

    assert book.mark_used('c1', 'm1', now=NOW) is True

    assert book.mark_used('c1', 'm1', now=NOW) is False

    assert book.used_keys(now=NOW) == frozenset({'c1|m1'})

    book.reset()            # 清镜像，逼它重新读盘

    assert book.used_keys(now=NOW) == frozenset({'c1|m1'})





def test_mark_used_needs_msg_id(book):

    assert book.mark_used('c1', '') is False

    assert book.used_keys(now=NOW) == frozenset()





def test_ledger_rolls_over_by_day(book):

    book.mark_used('c1', 'm1', now=NOW)

    assert book.used_keys(now=NOW + 3 * 86400) == frozenset()





def _sources():

    files = glob.glob(os.path.join(ROOT, 'loop', '**', '*.py'), recursive=True)

    files += glob.glob(os.path.join(ROOT, 'core', '**', '*.py'), recursive=True)

    out = {}

    for path in files:

        try:

            with open(path, encoding='utf-8') as fh:

                out[os.path.relpath(path, ROOT).replace('\\', '/')] = fh.read()

        except OSError:

            continue

    return out





def test_window_threshold_lives_in_one_place():
    # 语音的「10 分钟」现在问 core/infra/tempo.py（UI-066 乙），本文件不再自带数字；
    # `loop/media_barrier.py` 里那个 `WINDOW_S = 600.0` 是图像屏障在等「最近 10 分钟」
    # 的图解析完 —— 另一只钟（时效，不是人味），留在原地，硬合并会让两个窗口互相绑架。
    from core.infra import tempo

    assert vg.WINDOW_S == tempo.s("voice.window")
    hits = sorted(rel for rel, src in _sources().items()
                 if any(l.startswith('WINDOW_S = 600') for l in src.splitlines()))

    assert hits == ['loop/media_barrier.py'], hits

def test_gate_has_one_call_site():

    hits = sorted(rel for rel, src in _sources().items() if 'voice_gate.allow(' in src)

    assert hits == ['loop/voice_out.py'], hits





def test_ledger_has_one_writer():

    hits = sorted(rel for rel, src in _sources().items() if 'mark_used(' in src)

    assert hits == ['core/rules/voice_gate.py', 'loop/voice_out.py'], hits

