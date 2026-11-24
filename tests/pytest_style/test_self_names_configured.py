"""她的名字名单不能悄悄变空。

`core/identity/self_names.py` 按用户要求「不兜底、不从 contacts 自动补」：手写文件
`data/persona/me_names.json` 不存在或写空，群里就再也认不出「在叫她」，而且没有任何报错。
这里在文件存在的前提下锁住「至少有一个名字」，防止误删或写坏。
"""
from __future__ import annotations

import pytest

from core.identity import self_names as sn


def test_configured_names_are_not_empty():
    if not sn._MANUAL.exists():
        pytest.skip("本机没有 data/persona/me_names.json（按设计不兜底）")
    names = sn.self_names("data/platforms/wechat/test_yinyue/account.db", account_id="test_yinyue")
    assert names, "me_names.json 存在但没有有效名字：群里会认不出在叫她"
