# BR-069：说话前认人必须 fail-closed——认不出、认重了都拒绝，绝不猜。
#
# resolve_chat_id 的老行为允许 label.endswith(n) 的前后缀模糊匹配、并按字典顺序取第一个命中，
# 她只要说「鱼」就可能落到「大肥鱼」或「小肥鱼」里先出现的那个。say 这条路改走
# resolve_say_target（只用完全相等）。

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from loop import ports  # noqa: E402


def _fake(monkeypatch, mapping):
    monkeypatch.setattr(ports, "display_names", lambda db: dict(mapping))
    monkeypatch.setattr(ports, "_account_db", lambda: ":memory:")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _whitelist_off(monkeypatch):
    """这个文件只测「认人」的严格性（BR-069）；白名单（这个会话有没有往来）归 test_targets.py 管。

    2026-11-04：`resolve_say_target` 在返回之前多加了一道白名单（全 C 之后外发不再等点头，
    认得出不等于该发）。这里喂的是假 `display_names` + `:memory:` 库，没有任何往来记录 ——
    不关掉这道闸，认人的用例会被白名单按 fail-closed 挡住，测的就不是认人了。
    """
    monkeypatch.setattr(ports, "_say_target_allowed", lambda chat_id: (True, ""))


def test_empty_target_rejected():
    cid, why = ports.resolve_say_target("")
    assert cid == "" and "空" in why


def test_explicit_chat_id_passes():
    cid, why = ports.resolve_say_target("10000000001@chatroom")
    assert cid == "10000000001@chatroom" and why == ""
    cid, why = ports.resolve_say_target("wxid_abc123")
    assert cid == "wxid_abc123" and why == ""


def test_exact_name_unique_passes(monkeypatch):
    _fake(monkeypatch, {"a@chatroom": "大肥鱼"})
    cid, why = ports.resolve_say_target("大肥鱼")
    assert cid == "a@chatroom" and why == ""


def test_suffix_only_is_rejected(monkeypatch):
    _fake(monkeypatch, {"a@chatroom": "大肥鱼"})
    cid, why = ports.resolve_say_target("鱼")   # 老逻辑的 endswith 会命中这里
    assert cid == "" and "认不出" in why


def test_ambiguous_name_rejected(monkeypatch):
    _fake(monkeypatch, {"a@chatroom": "肥鱼", "b@chatroom": "肥鱼"})
    cid, why = ports.resolve_say_target("肥鱼")
    assert cid == "" and "不敢猜" in why


def test_brain_uses_strict_resolver_and_only_stops_on_success():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "loop", "brain.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "ports.resolve_say_target(" in src, "say 分支必须走严格认人"
    assert "if name == 'say':\n                stop = True" not in src, "被拦下的 say 不该结束这一拍"
    # 2026-11-02：试衣间（rehearsal）上线后 ok 判定多了 rehearsal 一档。
    # 2026-11-21：队列投递成功后回执是 sent；被 120 秒同文闸拦下时是 skipped（再试也发不出去）。
    # 这几种都算「这一拍已经有了结论」，被拒/出错仍必须能继续说明。
    say_gate = next(line for line in src.splitlines()
                    if line.strip().startswith('if name == "say" and') and 'receipt.get("result")' in line)
    assert '"blocked"' not in say_gate and '"error"' not in say_gate
    assert '"ok"' in say_gate and '"rehearsal"' in say_gate and '"sent"' in say_gate
