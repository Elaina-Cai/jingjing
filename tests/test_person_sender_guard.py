"""「谁算一个人」的机械闸：self / unknown / <群id>@chatroom 不许建出群成员。

2026-10-23 实测的病：互刷群名单 9 人 = 7 真人 + `self` + `unknown`，测试群 5 人 =
3 真人 + `unknown` + 一条以群 id 命名的「人」。根因是 `resolve_message` 把任何
非空 sender 当 wxid 建号，并且无条件 `record_membership_activity`。

本文件锁三件事：
* `is_person_sender` 的真值表（只认机械事实）；
* `resolve_message` 对这三类发送人**照旧**建身份（消息证据不丢），但不记群成员；
* 真的 wxid 照旧记成员、累加条数（别把闸门做成把正常人也挡了）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.repo_people import is_person_sender  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "_test_person_sender.db"
GROUP = "10000000001@chatroom"
SELF = "wxid_seed02"


def _cleanup(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def test_is_person_sender_truth_table() -> bool:
    print("\n[is_person_sender]")
    ok = True
    rejected = [
        "", "   ", None, "self", "SELF", "unknown", "Unknown", "none", "null",
        GROUP, "10000000004@chatroom", "123@im.chatroom", SELF,
    ]
    for value in rejected:
        got = is_person_sender(value, account_id=SELF)
        ok &= got is False
        if got:
            print(f"  FAIL 该拒却收了：{value!r}")
    accepted = [
        "wxid_seed01", "wxid_zhang", "zhangsan@openim",
        "group:1124196924",
    ]
    for value in accepted:
        got = is_person_sender(value, account_id=SELF)
        ok &= got is True
        if not got:
            print(f"  FAIL 该收却拒了：{value!r}")
    print(f"  ok   拒 {len(rejected)} 个 / 收 {len(accepted)} 个")
    return ok


def _repo():
    from db.meta_repo import MetaRepositoryManager

    return MetaRepositoryManager(str(_DB))


def test_junk_senders_do_not_become_members() -> bool:
    print("\n[不记成员]")
    _cleanup(_DB)
    ok = True
    try:
        repo = _repo()
        people = repo.people
        cases = {
            "self": {"sender_id": "self", "sender_name": "鲸鲸"},
            "unknown": {"sender_id": "unknown", "sender_name": "unknown"},
            "group-id": {"sender_id": GROUP, "sender_name": "示例作业群"},
        }
        for label, extra in cases.items():
            person = people.resolve_message({
                "msg_id": f"junk-{label}",
                "chat_id": GROUP,
                "chat_type": "group",
                "source": "wechat",
                "account_id": SELF,
                **extra,
            })
            ok &= person is not None
            left = people.list_memberships(person.person_id, chat_id=GROUP) if person else []
            ok &= len(left) == 0
            if left:
                print(f"  FAIL {label} 被记成群成员了")
            else:
                print(f"  ok   {label}：有身份、没成员关系")
    finally:
        engine = getattr(repo, "engine", None)
        if engine is not None:
            engine.dispose()
        _cleanup(_DB)
    return ok


def test_real_wxid_still_records_membership() -> bool:
    print("\n[真人照旧]")
    _cleanup(_DB)
    ok = True
    try:
        repo = _repo()
        people = repo.people
        first = people.resolve_message({
            "msg_id": "real-1",
            "sender_id": "wxid_zhang",
            "sender_name": "张三",
            "chat_id": GROUP,
            "chat_type": "group",
            "source": "wechat",
            "account_id": SELF,
        })
        second = people.resolve_message({
            "msg_id": "real-2",
            "sender_id": "wxid_zhang",
            "sender_name": "张三",
            "chat_id": GROUP,
            "chat_type": "group",
            "source": "wechat",
            "account_id": SELF,
        })
        ok &= first is not None and second is not None
        ok &= first.person_id == second.person_id
        memberships = people.list_memberships(first.person_id, chat_id=GROUP)
        ok &= len(memberships) == 1
        ok &= memberships[0].message_count == 2
        ok &= memberships[0].group_nickname == "张三"
        print(f"  ok   真人一条成员关系、条数 {memberships[0].message_count}")
    finally:
        engine = getattr(repo, "engine", None)
        if engine is not None:
            engine.dispose()
        _cleanup(_DB)
    return ok


def main() -> bool:
    tests = [
        test_is_person_sender_truth_table,
        test_junk_senders_do_not_become_members,
        test_real_wxid_still_records_membership,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            if not test():
                failed.append(test.__name__)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
            failed.append(test.__name__)
    print("\n" + ("FAILED: " + ", ".join(failed) if failed else "ALL PASSED"))
    return not failed


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)