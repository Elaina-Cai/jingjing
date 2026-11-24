"""批准请求只送「主人本人的私聊」。

真机 2026-10-21 业主原话：「为什么不是私聊她，我都在群里说了还要她说什么」——
问句被拼进了群里那条回复。三道检查：
1. 判据本身（owner 私聊 = True，群/空 = False，查询失败 = False 不猜）；
2. 三个追加点（turn_pipeline 1956/1970/2027 附近）都套上了这道闸；
3. 闸是「宁可不问」：查不到主人就是 False。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import ask  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + ((" :: " + str(detail)) if detail else ""))


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, rows) -> None:
        self._rows = rows

    def execute(self, *_a, **_k):
        return _Result(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Engine:
    def __init__(self, rows) -> None:
        self._rows = rows

    def connect(self):
        return _Conn(self._rows)


class _BoomEngine:
    def connect(self):
        raise RuntimeError("库不在")


def t_owner_rows() -> None:
    eng = _Engine([("wxid_owner",), ("1768136519",)])
    check("查出两个主人地址", ask.owner_private_chats(engine=eng) == ("wxid_owner", "1768136519"), ask.owner_private_chats(engine=eng))
    check("私聊判 True", ask.is_owner_private("wxid_owner", engine=eng) is True)
    check("群判 False", ask.is_owner_private("123@chatroom", engine=eng) is False)
    check("空串判 False", ask.is_owner_private("", engine=eng) is False)
    check("空白行被丢", ask.owner_private_chats(engine=_Engine([("  ",), ("x",)])) == ("x",))


def t_fail_closed() -> None:
    check("查询炸了 → 空元组", ask.owner_private_chats(engine=_BoomEngine()) == ())
    check("查询炸了 → 不算私聊", ask.is_owner_private("wxid_owner", engine=_BoomEngine()) is False)


def t_read_only_engine() -> None:
    src = (ROOT / "core" / "jobs" / "ask.py").read_text(encoding="utf-8")
    check("用只读句柄", "read_only_engine" in src)
    check("不拿可写 engine", "get_meta_repo().engine" not in src)


def t_call_sites_guarded() -> None:
    # 2026-10-23：「补问句」只剩一个 owner（core/speech/plan_ingest.py）。原来路由侧
    # 有三处并行实现、5 次 is_owner_private(chat_id)，其中一处还不记账——同一句话可能
    # 被补两句问句，且哪一处生效取决于走的是直回还是触发路径。
    src = (ROOT / "core" / "speech" / "plan_ingest.py").read_text(encoding="utf-8")
    router_src = (ROOT / "core" / "routing" / "turn_pipeline.py").read_text(encoding="utf-8")
    check("唯一追加点套闸", "is_owner_private(chat_id)" in src, src.count("is_owner_private(chat_id)"))
    check("被拦时留一行日志", "job_ask_deferred" in src)
    check("没有裸的 pending_asks 循环", "for _item in _ask.pending_asks(" not in src)
    check("路由侧不再自己追问", "pending_asks(" not in router_src)
    check("出口调这个唯一实现", "plan_ingest.append_pending_ask(" in
          (ROOT / "core" / "speech" / "outbound.py").read_text(encoding="utf-8"))


def main() -> int:
    for fn in (t_owner_rows, t_fail_closed, t_read_only_engine, t_call_sites_guarded):
        print("\n" + fn.__name__)
        fn()
    print(f"\nRESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
