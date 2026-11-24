# -*- coding: utf-8 -*-
r"""身份键闸（``scripts/check_identity_keys.py``）的测试。

四条规则各有一个**合成违规样本**：闸必须抓到它。另外两条边界：
* 类属性 ``conversation_key``（三元组别名）不算"第二处串化"；
* ``utterance_edges`` 的已知缺口在 ``KNOWN_MISSING_ACCOUNT`` 里，不报错但要数得出来。

跑法：``.venv_it\\Scripts\\python.exe -X utf8 -u tests/test_check_identity_keys.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import check_identity_keys as chk  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：mkdtemp 不给 dir= 时系统 TEMP 不可写会静默回落到仓库根。
from _tempdirs import make_db_dir  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED += 1
        print(f"FAIL {name}" + (f"  [{detail}]" if detail else ""))


def _sandbox(files: dict[str, str]) -> Path:
    root = make_db_dir("idkeys-")
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _scan(files: dict[str, str]):
    root = _sandbox(files)
    pairs = [
        (p, p.relative_to(root).as_posix())
        for p in sorted(root.rglob("*.py"))
    ]
    return pairs


def t_rule_a_catches_unscoped_window() -> None:
    files = _scan({
        "core/context/bad.py": "SQL = 'select * from terminal_events order by seq'\n",
        "core/context/good.py": "SQL = 'select * from terminal_events where chat_id = :chat_id'\n",
    })
    problems = chk.check_window_scoped(files)
    check("A 抓到没按会话取的窗口读", len(problems) == 1 and "bad.py" in problems[0], str(problems))
    check("A 不误报按 chat_id 取的", all("good.py" not in p for p in problems))


def t_rule_b_catches_person_as_chat_key() -> None:
    body = (
        "class Bad:\n"
        '    __tablename__ = "chat_members"\n'
        '    __table_args__ = (UniqueConstraint("person_id", "chat_id", name="ux_x"),)\n'
    )
    files = _scan({"db/bad.py": body})
    problems = chk.check_person_chat_keys(files)
    check("B 抓到拿人当会话键", len(problems) == 1 and "chat_members" in problems[0], str(problems))

    ok = (
        "class Ok:\n"
        '    __tablename__ = "group_memberships"\n'
        '    __table_args__ = (UniqueConstraint("person_id", "chat_id", name="ux_y"),)\n'
    )
    problems = chk.check_person_chat_keys(_scan({"db/ok.py": ok}))
    check("B 白名单里的「人∈会话」不报", not problems, str(problems))


def t_rule_c_single_owner() -> None:
    files = _scan({
        "core/context/chat_window.py": "def conversation_key(p, a, c):\n    return ''\n",
        "core/other.py": "def conversation_key(p, a, c):\n    return '|'.join((p, a, c))\n",
    })
    problems = chk.check_single_key_owner(files)
    check("C 抓到第二处串化", any("core/other.py" in p for p in problems), str(problems))
    check("C 拥有者不报", all(not p.startswith("C core/context/chat_window.py:") for p in problems), str(problems))

    alias = _scan({
        "core/context/chat_window.py": "def conversation_key(p, a, c):\n    return ''\n",
        "core/contracts/events.py": (
            "class M:\n    @property\n    def conversation_key(self):\n        return ()\n"
        ),
    })
    check("C 类属性别名不算串化", not chk.check_single_key_owner(alias))


def t_rule_d_unique_needs_platform_and_account() -> None:
    bad = (
        "class Bad:\n"
        '    __tablename__ = "utterance_edges"\n'
        '    __table_args__ = (UniqueConstraint("platform", "chat_id", "msg_id",'
        ' name="ux_new_edge"),)\n'
    )
    problems = chk.check_unique_scoped(_scan({"db/bad.py": bad}))
    check("D 抓到键里缺账号", len(problems) == 1 and "缺 account_id" in problems[0], str(problems))

    known = (
        "class Known:\n"
        '    __tablename__ = "utterance_edges"\n'
        '    __table_args__ = (UniqueConstraint("platform", "chat_id", "msg_id",'
        ' name="ux_utterance_edge_msg"),)\n'
    )
    # 2026-10-21：utterance_edges 的唯一键补上 bot_account_id（迁移 66），
    # KNOWN_MISSING_ACCOUNT 清空 ⇒ 规则 D 现在必须**零缺口**。这条从「允许登记」
    # 反过来写：缺口必须被报出、登记表必须为空，缺回来时才有信号。
    _d = chk.check_unique_scoped(_scan({"db/known.py": known}))
    check("D 缺口必须报出且登记表为空",
          len(_d) == 1 and not chk.KNOWN_MISSING_ACCOUNT, str(_d))

    good = (
        "class Good:\n"
        '    __tablename__ = "messages"\n'
        '    __table_args__ = (UniqueConstraint("account_id", "source", "chat_id",'
        ' "msg_id", name="ux_ok"),)\n'
    )
    check("D 四个维度齐了就不报", not chk.check_unique_scoped(_scan({"db/good.py": good})))


def t_repo_is_clean() -> None:
    problems = chk.run()
    check("仓库当前零违规（规则 A–D + parity）", not problems, str(problems[:3]))


def t_parity() -> None:
    check("parity 串化 == ConversationRef.key 的 join", not chk.check_parity())


def t_allowlist_is_documented() -> None:
    check("B 白名单每条都有理由",
          all(str(v).strip() for v in chk.PERSON_CHAT_TABLES.values()))
    check("D 已知缺口每格都有理由",
          all(str(v).strip() for v in chk.KNOWN_MISSING_ACCOUNT.values()))


def t_chat_type_checked() -> None:
    """② 字段优先、形状兜底；冲突记一笔但不改判。"""
    from core.contracts.events import ChatType
    from core.identity import platforms as pl

    pl.reset_mismatches()
    check("字段优先", pl.chat_type_checked("wechat", "group", "wxid-friend") == ChatType.GROUP)
    check("形状兜底", pl.chat_type_checked("wechat", "", "123@chatroom") == ChatType.GROUP)
    check("都说不出 → UNKNOWN", pl.chat_type_checked("wechat", "", "wxid-x") == ChatType.UNKNOWN)

    pl.reset_mismatches()
    pl.chat_type_checked("wechat", "private", "123@chatroom")
    counts = pl.mismatch_counts()
    check("冲突记一笔", sum(counts.values()) == 1, str(counts))
    check("冲突时仍听字段（不改判）",
          pl.chat_type_checked("wechat", "private", "123@chatroom") == ChatType.PRIVATE)
    pl.chat_type_checked("wechat", "private", "123@chatroom")
    check("同一冲突只占一格、计数累加",
          list(pl.mismatch_counts().values()) == [3], str(pl.mismatch_counts()))
    pl.reset_mismatches()
    check("reset 清空", not pl.mismatch_counts())


def main() -> int:
    for fn in (
        t_rule_a_catches_unscoped_window,
        t_rule_b_catches_person_as_chat_key,
        t_rule_c_single_owner,
        t_rule_d_unique_needs_platform_and_account,
        t_repo_is_clean,
        t_parity,
        t_allowlist_is_documented,
        t_chat_type_checked,
    ):
        fn()
    print()
    print(f"RESULT {FAILED} failed / {PASSED + FAILED} checked")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
