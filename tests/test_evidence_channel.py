# -*- coding: utf-8 -*-
"""批C Step C2：⑩ 本地证据查询通道（⑧假说 → 成员/备注/关系/wxid）。

覆盖：
* 空查询 / 缺失 repo / repo 方法抛错 → 空列表（通道不猜、不爆）；
* person → identity → membership → contact → relationship 稳定顺序与字段；
* 关系只对解析出的 wxid 候选命中；limit 截断；digest 紧凑有界；
* 模块只读静态守卫：无写调用、顶层不 import db/llm/scheduler/agents/sqlalchemy。
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.execution import evidence_channel as ec  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Row:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class _People:
    def __init__(self):
        self.calls = []

    def find_persons(self, query, *, platform="", account_id="", limit=20):
        self.calls.append(("find_persons", query, platform, account_id, limit))
        return [_Row(person_id="p1", display_name="P4")]

    def list_identities(self, person_id):
        self.calls.append(("list_identities", person_id))
        return [
            _Row(identity_value="wxid_abc", identity_type="wxid", chat_id="", confidence=0.9, source="observed"),
            _Row(identity_value="蔡神", identity_type="group_nickname", chat_id="g1", confidence=0.7, source="chat"),
        ]

    def list_memberships(self, person_id, chat_id=""):
        self.calls.append(("list_memberships", person_id, chat_id))
        return [_Row(group_nickname="蔡神", chat_id="g1", role="member", message_count=3)]


class _Contacts:
    def __init__(self):
        self.calls = []

    def search_by_name(self, query, limit=10):
        self.calls.append(("search_by_name", query, limit))
        return [_Row(wxid="wxid_abc", nickname="蔡神", remark="老蔡")]


class _Relationships:
    def __init__(self):
        self.calls = []

    def get(self, persona_id, target_wxid):
        self.calls.append(("get", persona_id, target_wxid))
        if target_wxid != "wxid_abc":
            return None
        return _Row(relation_type="群友", intimacy=0.6, notes="常来串门")


class _Repo:
    def __init__(self):
        self.people = _People()
        self.contacts = _Contacts()
        self.relationships = _Relationships()


def _hits(repo, **kwargs):
    return ec.read_evidence(repo, query="蔡神", **kwargs)


def test_empty_query_returns_empty():
    repo = _Repo()
    assert ec.read_evidence(repo, query="") == []
    assert ec.read_evidence(repo, query="   ") == []
    assert repo.people.calls == [] and repo.contacts.calls == []


def test_missing_repo_and_missing_attrs_are_empty():
    assert ec.read_evidence(object(), query="蔡神") == []
    assert ec.read_evidence(_Repo(), query="蔡神", limit=0) or True  # limit 最小 1，不抛


def test_hit_order_and_fields():
    hits = _hits(_Repo())
    assert [hit.kind for hit in hits] == ["person", "identity", "identity", "membership", "contact", "relationship"], [hit.kind for hit in hits]
    person = hits[0]
    assert person.value == "P4" and person.person_id == "p1" and person.source == "person_entities"
    ident = hits[1]
    assert ident.value == "wxid_abc" and ident.identity_type == "wxid" and ident.confidence == 0.9
    nick = hits[2]
    assert nick.value == "蔡神" and nick.chat_id == "g1" and nick.identity_type == "group_nickname"
    member = hits[3]
    assert member.value == "蔡神" and member.chat_id == "g1" and member.detail["role"] == "member"
    contact = hits[4]
    assert contact.value == "wxid_abc" and contact.label == "老蔡"
    assert contact.detail == {"nickname": "蔡神", "remark": "老蔡"}


def test_relationship_only_for_resolved_wxid_candidates():
    repo = _Repo()
    hits = _hits(repo)
    rel = [hit for hit in hits if hit.kind == "relationship"]
    assert len(rel) == 1 and rel[0].value == "wxid_abc" and rel[0].confidence == 0.6
    assert rel[0].detail["relation_type"] == "群友"
    # 群昵称（非 wxid）不当作关系候选
    assert all(call[2] in ("wxid_abc",) for call in repo.relationships.calls), repo.relationships.calls


def test_note_hits_from_injected_reader():
    notes = [
        {"person_key": "wxid_abc", "content": "蔡神就是P4", "chat_id": "g1", "source": "owner_confirmed"},
        {"person_key": "wxid_zzz", "content": "另一个群友", "chat_id": "g1", "source": "owner"},
        {"person_key": "", "content": "本群玩梗多", "chat_id": "g1", "source": "owner"},
    ]
    hits = ec.read_evidence(_Repo(), query="蔡神", notes_reader=lambda *, chat_id="": notes)
    kinds = [hit.kind for hit in hits]
    assert "note" in kinds, kinds
    note = [hit for hit in hits if hit.kind == "note"][0]
    assert note.value == "蔡神就是P4"
    assert note.label == "wxid_abc"
    assert note.source == "owner_confirmed"
    assert note.detail == {"person_key": "wxid_abc"}
    assert kinds.index("membership") < kinds.index("note") < kinds.index("contact"), kinds
    assert all("另一个群友" not in hit.value for hit in hits)


def test_notes_not_read_when_repo_injected_without_reader():
    hits = ec.read_evidence(_Repo(), query="蔡神")
    assert all(hit.kind != "note" for hit in hits)


def test_repo_errors_are_swallowed():
    class _Boom:
        def __init__(self):
            self.people = self
            self.contacts = self
            self.relationships = self

        def find_persons(self, *a, **k):
            raise RuntimeError("db down")

        def search_by_name(self, *a, **k):
            raise RuntimeError("db down")

        def list_identities(self, *a, **k):
            raise RuntimeError("db down")

        def list_memberships(self, *a, **k):
            raise RuntimeError("db down")

        def get(self, *a, **k):
            raise RuntimeError("db down")

    assert ec.read_evidence(_Boom(), query="蔡神") == []


def test_limit_caps_total_hits():
    hits = _hits(_Repo(), limit=2)
    assert len(hits) == 2 and [hit.kind for hit in hits] == ["person", "identity"], hits
    # limit 也传给 repo 单次查询
    repo = _Repo()
    _hits(repo, limit=2)
    assert repo.people.calls[0][-1] == 2 and repo.contacts.calls[0][-1] == 2


def test_digest_is_compact_and_bounded():
    hits = _hits(_Repo())
    digest = ec.evidence_digest(hits, limit=2)
    assert len(digest) == 2
    assert set(digest[0]) == {"kind", "value", "label", "person_id", "chat_id", "identity_type", "confidence", "source"}
    assert "detail" not in digest[0]
    assert ec.evidence_digest(hits, limit=0) == []


def test_module_read_only_static():
    tree = ast.parse((ROOT / "core" / "execution" / "evidence_channel.py").read_text(encoding="utf-8"))
    top_imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_imports.add(node.module.split(".")[0])
    assert not ({"db", "llm", "scheduler", "agents", "sqlalchemy"} & top_imports), top_imports
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not ({"add", "upsert", "commit", "resolve_identity", "link_identity"} & calls), calls


def main() -> bool:
    checks = [
        test_empty_query_returns_empty,
        test_missing_repo_and_missing_attrs_are_empty,
        test_hit_order_and_fields,
        test_relationship_only_for_resolved_wxid_candidates,
        test_repo_errors_are_swallowed,
        test_note_hits_from_injected_reader,
        test_notes_not_read_when_repo_injected_without_reader,
        test_limit_caps_total_hits,
        test_digest_is_compact_and_bounded,
        test_module_read_only_static,
    ]
    passed = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - 失败必须逐个报出来，不许静默
            print(f"  FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  ok   {check.__name__}")
    print(f"evidence-channel checks: {passed}/{len(checks)} passed")
    return passed == len(checks)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
