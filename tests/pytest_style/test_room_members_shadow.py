"""群成员旁路表的锁：能不能用、有没有越界。

两件事都要钉住：

1. **能查**：给它一份解密副本，它要能算出「这个屋里此刻有谁」，并且
   「不知道」与「不在」必须分得开（``None`` vs ``False``）——否则 BR-069
   的 ``absent`` 就会把「这张表还没同步过这个屋」当成「人不在这里」。
2. **不越界**：``core/context/room_members.py`` 只许写自己的旁路库。
   一旦它开始碰 ``group_memberships`` / ``person_notes``，203 个沉默成员就会
   变成她"认识"的人（进提示词、可能被 @），这正是这一层要避免的事。
"""

from __future__ import annotations

import ast
import sqlite3
import time
from pathlib import Path

from core.context import room_members
from core.execution import wx_roster

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "core" / "context" / "room_members.py"
FORBIDDEN = ("group_memberships", "person_notes", "GroupMembership", "meta_models")


def _fake_decrypted(path: Path, rooms: dict[str, dict[str, str]], owner: str = "") -> Path:
    """造一份形状与真副本相同的 ``contact.db``（只建用到的列）。"""
    con = sqlite3.connect(str(path))
    con.executescript(
        "create table chat_room (id integer primary key, username text, owner text);"
        "create table contact (id integer primary key, username text, nick_name text);"
        "create table chatroom_member (room_id integer, member_id integer);"
    )
    seq = 0
    for rid, (room, people) in enumerate(rooms.items(), 1):
        con.execute("insert into chat_room values (?,?,?)", (rid, room, owner))
        for wxid, nick in people.items():
            seq += 1
            cid = 1000 + seq
            con.execute("insert into contact values (?,?,?)", (cid, wxid, nick))
            con.execute("insert into chatroom_member values (?,?)", (rid, cid))
    con.commit()
    con.close()
    return path


def _members_fixture(tmp_path: Path) -> Path:
    return _fake_decrypted(
        tmp_path / "contact.db",
        {"111@chatroom": {"wxid_a": "甲", "wxid_b": ""}, "222@chatroom": {"wxid_c": "丙"}},
        owner="wxid_a",
    )


def test_reads_the_decrypted_copy(tmp_path: Path) -> None:
    got = wx_roster.read_decrypted(_members_fixture(tmp_path))
    assert sorted(got) == ["111@chatroom", "222@chatroom"]
    assert sorted(got["111@chatroom"]) == ["wxid_a", "wxid_b"]
    assert got["111@chatroom"]["wxid_a"] == "甲"
    assert got["111@chatroom"]["wxid_b"] == "wxid_b", "昵称空着要退回 wxid，不能变空串"


def test_upsert_is_idempotent_and_never_deletes(tmp_path: Path) -> None:
    con = room_members.connect(tmp_path / "roster.db")
    first = room_members.write_rows(
        {"111@chatroom": {"wxid_a": "甲", "wxid_b": "乙"}}, synced_at="2026-10-29 10:00:00", con=con
    )
    again = room_members.write_rows(
        {"111@chatroom": {"wxid_a": "甲", "wxid_b": "乙"}}, synced_at="2026-10-29 11:00:00", con=con
    )
    assert first == again == {"rooms": 1, "members": 2}
    assert con.execute("select count(*) from room_member").fetchone()[0] == 2

    room_members.write_rows({"111@chatroom": {"wxid_a": "甲"}}, synced_at="2026-10-29 12:00:00", con=con)
    assert room_members.members("111@chatroom", con=con) == {"wxid_a"}
    assert room_members.members("111@chatroom", listed_only=False, con=con) == {"wxid_a", "wxid_b"}
    con.close()


def test_unknown_room_is_unknown_not_absent(tmp_path: Path) -> None:
    con = room_members.connect(tmp_path / "roster.db")
    room_members.write_rows({"111@chatroom": {"wxid_a": "甲"}}, con=con)
    assert room_members.contains("111@chatroom", "wxid_a", con=con) is True
    assert room_members.contains("111@chatroom", "wxid_zzz", con=con) is False
    assert room_members.contains("999@chatroom", "wxid_a", con=con) is None
    con.close()


def test_owner_is_marked(tmp_path: Path) -> None:
    con = room_members.connect(tmp_path / "roster.db")
    room_members.write_rows(
        {"111@chatroom": {"wxid_a": "甲", "wxid_b": "乙"}},
        owners={"111@chatroom": "wxid_a"},
        con=con,
    )
    flags = dict(con.execute("select wxid, is_owner from room_member"))
    assert flags == {"wxid_a": 1, "wxid_b": 0}
    con.close()


def test_fresh_needs_a_parseable_stamp(tmp_path: Path) -> None:
    con = room_members.connect(tmp_path / "roster.db")
    assert room_members.fresh("111@chatroom", max_age_ms=10, con=con) is False
    room_members.write_rows({"111@chatroom": {"wxid_a": "甲"}}, synced_at="看不懂", con=con)
    assert room_members.fresh("111@chatroom", max_age_ms=10**12, con=con) is False
    room_members.write_rows({"111@chatroom": {"wxid_a": "甲"}}, synced_at="2026-10-29 10:00:00", con=con)
    assert room_members.fresh("111@chatroom", max_age_ms=10**12, con=con) is True
    assert room_members.fresh("111@chatroom", max_age_ms=1, con=con) is False
    con.close()


def test_module_never_touches_her_memory() -> None:
    text = MODULE.read_text(encoding="utf-8")
    body = text.split('"""', 2)[-1]
    for word in FORBIDDEN:
        assert word not in body, "%s: 旁路表越界了，沉默成员会变成她认识的人" % word
    tree = ast.parse(text)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [m for m in imported if m == "db" or m.startswith("db.")], imported


def test_default_db_sits_beside_the_other_data(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(room_members.ENV_PATH, raising=False)
    assert room_members.db_path().as_posix().endswith("data/roster_shadow.db")
    monkeypatch.setenv(room_members.ENV_PATH, str(tmp_path / "x.db"))
    assert room_members.db_path() == tmp_path / "x.db"


def test_generic_layer_knows_no_platform_words():
    """通用层（core/context）不许出现平台/产品词——平台相关的读取在适配层。

    这条是 docs/控制元规则.md 那条边界锁的本地版：那边一次扫全仓、跑得慢；
    这里只钉本模块，改一行就知道有没有越界。
    """
    src = (ROOT / "core" / "context" / "room_members.py").read_text(encoding="utf-8").lower()
    words = ("微信", "wechat", "chatroom", "contact.db", "decrypted", "onebot")
    hits = [w for w in words if w in src]
    assert not hits, "通用层出现了平台词：%s（应放进 core/execution/wx_roster.py）" % hits


def test_adapter_is_the_only_reader_of_the_copy():
    """三张表的读取只许在适配层出现一次；通用层不许自己读解密副本。"""
    defs = []
    for path in (ROOT / "core").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "chatroom_member" in text:
            defs.append(path.relative_to(ROOT).as_posix())
    assert defs == ["core/execution/wx_roster.py"], defs


def test_resolve_says_in_out_and_unsure(tmp_path) -> None:
    """三态各一次：wxid/昵称对上算在册，陌生人不算，没同步过的房间说不准。"""
    from core.context import room_members as rm

    con = rm.connect(tmp_path / "shadow.db")
    try:
        rm.write_rows(
            {"room-1": {"wxid_a": "阿甲", "wxid_b": "阿乙"}},
            owners={"room-1": "wxid_a"},
            con=con,
        )
        assert rm.resolve("room-1", "wxid_a", con=con)[0] == rm.VERDICT_IN
        assert rm.resolve("room-1", "阿乙", con=con)[0] == rm.VERDICT_IN
        assert rm.resolve("room-1", "路人", con=con)[0] == rm.VERDICT_OUT
        assert rm.resolve("room-9", "阿甲", con=con)[0] == rm.VERDICT_UNSURE
        assert rm.resolve("", "阿甲", con=con)[0] == rm.VERDICT_UNSURE
    finally:
        con.close()


def test_resolve_is_unsure_when_the_snapshot_is_old(tmp_path) -> None:
    """旧快照不能拿来判人：说「不在册」会把她变成哑巴。"""
    from core.context import room_members as rm

    con = rm.connect(tmp_path / "shadow.db")
    try:
        rm.write_rows(
            {"room-1": {"wxid_a": "阿甲"}},
            synced_at="2020-01-01 00:00:00",
            con=con,
        )
        assert rm.resolve("room-1", "路人", con=con)[0] == rm.VERDICT_UNSURE
    finally:
        con.close()


def test_both_send_paths_ask_the_roster_once_each() -> None:
    """文字路与文件路各问一次（少一就是漏一扇门，多一就是两套口径）。"""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("core/speech/outbound.py").read_text(encoding="utf-8"))
    defined = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name == "_roster_target_error"]
    assert len(defined) == 1, "判据应只有一个定义"
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_roster_target_error"]
    assert len(calls) == 2, "应恰好两处调用（文字 / 文件）"


def test_shadow_rows_never_make_people() -> None:
    """丙：旁路表只回答「在不在」，不许替她「认识人」。

    读口与判据之外，谁也不许把人写进 ``person_identities`` / ``group_memberships`` /
    ``person_notes`` —— 新人进她认识的人，只有主人确认这一条路。
    """
    import re
    from pathlib import Path

    forbidden = ("person_identities", "group_memberships", "person_notes")
    for rel in ("core/context/room_members.py", "core/execution/wx_roster.py",
                "scripts/sync_wx_roster.py"):
        src = Path(rel).read_text(encoding="utf-8")
        for word in forbidden:
            assert not re.search(r"insert\s+into\s+%s|update\s+%s|delete\s+from\s+%s"
                                 % (word, word, word), src, re.I), "%s 碰了 %s" % (rel, word)


def test_only_the_gate_and_the_reader_may_import_the_shadow() -> None:
    """旁路表的合法读者只有三个：发送口（判据）、同步脚本（写入口）、巡检（只读汇总）。

    巡检算合法读者，因为它只汇总「几个群 / 几人 / 最近判成不在册几次」，
    既不参与发送判定，也不改名单、不建人 —— 而观测必须有一个能读的出口，
    否则 BR-069 的「先留痕再开闸」就没有眼睛。多一个读者就是漏。

    只看真正的 ``import``（ast），不看注释里提到模块名 —— 否则写一行说明就撞锁。
    """
    import ast
    from pathlib import Path

    allowed = {"core/speech/outbound.py", "scripts/sync_wx_roster.py", "console/checkup.py",
        "loop/brief.py",  # 提示词展示：这屋里都有谁（只读，不判定）
    }
    found = set()
    for root in ("core", "scripts", "loop", "console", "agents"):
        for path in Path(root).rglob("*.py"):
            rel = path.as_posix()
            if rel == "core/context/room_members.py":
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:  # noqa: BLE001 - 解析不了要吵，不许当没看见
                raise AssertionError("%s 解析不了：%s" % (rel, exc))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.ImportFrom):
                    # 函数里的 `from core.context import room_members as _rooms`
                    # 别名在 node.names 里，不在 node.module 里 —— 只查 module 会扫空。
                    names = [node.module or ""] + [a.name for a in node.names]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                if any(n == "room_members" or n.endswith(".room_members") for n in names):
                    found.add(rel)
    assert found <= allowed, "多出来的引用方：%s" % sorted(found - allowed)


def _shadow_now(tmp_path: Path, rows: dict[str, dict[str, str]]) -> sqlite3.Connection:
    con = room_members.connect(tmp_path / "roster.db")
    room_members.write_rows(rows, synced_at=time.strftime("%Y-%m-%d %H:%M:%S"), con=con)
    return con


def test_decoration_does_not_hide_a_member(tmp_path: Path) -> None:
    con = _shadow_now(tmp_path, {"111@chatroom": {"wxid_a": "\u0f04\u0f0a\u5c0f\u660e\ua9bf", "wxid_b": "\u963f \u4e09"}})
    assert room_members.resolve("111@chatroom", "\u5c0f\u660e", con=con) == (
        room_members.VERDICT_IN, "\u5199\u6cd5\u5f52\u4e00\u540e\u5bf9\u4e0a")
    assert room_members.resolve("111@chatroom", "\u963f\u4e09", con=con)[0] == room_members.VERDICT_IN


def test_two_same_key_candidates_are_unsure(tmp_path: Path) -> None:
    con = _shadow_now(tmp_path, {"111@chatroom": {"wxid_a": "\u5c0f\u660e\uff08\u6302\u673a\uff09", "wxid_b": "\u5c0f\u660e\uff08\u5728\uff09"}})
    verdict, why = room_members.resolve("111@chatroom", "\u5c0f\u660e", con=con)
    assert verdict == room_members.VERDICT_UNSURE, why


def test_prefix_candidate_is_unsure_not_absent(tmp_path: Path) -> None:
    con = _shadow_now(tmp_path, {"111@chatroom": {"wxid_a": "\u5c0f\u660e\uff08\u6302\u673a\uff09"}})
    verdict, why = room_members.resolve("111@chatroom", "\u5c0f\u660e", con=con)
    assert verdict == room_members.VERDICT_UNSURE and "\u5f00\u5934" in why, why


def test_name_without_letters_or_digits_is_unsure(tmp_path: Path) -> None:
    con = _shadow_now(tmp_path, {"111@chatroom": {"wxid_a": "\u5c0f\u660e"}})
    assert room_members.resolve("111@chatroom", "\U0001f642", con=con)[0] == room_members.VERDICT_UNSURE


def test_out_ledger_records_and_counts(tmp_path: Path) -> None:
    con = _shadow_now(tmp_path, {"111@chatroom": {"wxid_a": "\u5c0f\u660e"}})
    assert room_members.resolve("111@chatroom", "\u67e5\u65e0\u6b64\u4eba", con=con)[0] == room_members.VERDICT_OUT
    assert room_members.count_outs(hours=24, con=con) == 0, "\u5224\u636e\u672c\u8eab\u4e0d\u8bb8\u5199\u53f0\u8d26"
    room_members.note_out("111@chatroom", "\u67e5\u65e0\u6b64\u4eba", "\u540d\u5355\u91cc\u6ca1\u6709", con=con)
    assert room_members.count_outs(hours=24, con=con) == 1
    got = room_members.recent_outs(hours=24, con=con)
    assert got and got[0][1] == "\u67e5\u65e0\u6b64\u4eba"


def test_checkup_reports_the_roster(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(room_members.ENV_PATH, str(tmp_path / "roster.db"))
    con = _shadow_now(tmp_path, {"111@chatroom": {"wxid_a": "\u5c0f\u660e"}})
    con.close()
    from console.checkup import check_roster

    row = check_roster()
    assert row["level"] in ("ok", "warn"), row
    assert "\u4e2a\u7fa4" in row["line"] and "\u4e0d\u5728\u518c" in row["line"]
