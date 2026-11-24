# -*- coding: utf-8 -*-
"""「请求→回复→@对象」链回归（2026-09-28 @错人修法）。

覆盖：派生（parent/recipient）、标注、成对补行、去重优先留 reply 分身、开关回退。

Run directly:  python tests/test_answer_links.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.context.answer_links import (  # noqa: E402
    annotate_candidate_line,
    augment_rows_with_links,
    derive_answer_links,
    links_by_request,
)
from core.context.chat_window import WindowMessage, _dedupe_self_echoes  # noqa: E402

BASE = datetime(2026, 9, 28, 13, 23, 0)


def _msg(
    minutes: float,
    speaker: str,
    content: str,
    *,
    is_self: bool = False,
    is_pat: bool = False,
    is_revoke: bool = False,
    event_type: str = "message",
    event_id: str = "",
    msg_id: str = "",
    parent: str = "",
    recipients: tuple[str, ...] = (),
    author_id: str = "",
) -> WindowMessage:
    return WindowMessage(
        occurred_at=BASE + timedelta(minutes=minutes),
        speaker=speaker,
        content=content,
        is_self=is_self,
        is_pat=is_pat,
        is_revoke=is_revoke,
        event_type=event_type,
        event_id=event_id,
        msg_id=msg_id,
        parent_msg_id=parent,
        recipients=recipients,
        author_id=author_id,
    )


def _rows():
    request = _msg(
        1, "Elaina", "帮我搜索有没有可以把图片转换为拼豆设计图的工具@鲸鲸",
        event_type="request", event_id="ev_req", msg_id="m_req", author_id="wxid_seed01",
    )
    reply = _msg(
        2, "鲸鲸", "@Elaina 有，直接说几个能用的：Webfem…",
        is_self=True, event_type="reply", event_id="ev_reply",
        parent="m_req", recipients=("wxid_seed01",),
    )
    pat = _msg(2.5, "P5", '"P5" 拍了拍 "鲸鲸"', is_pat=True, event_id="ev_pat", msg_id="m_pat")
    pat_reply = _msg(
        3, "鲸鲸", "哎呀，P5，都被拍醒了！", is_self=True, event_type="reply",
        event_id="ev_pat_reply", parent="m_pat", recipients=("wxid_seed05",),
    )
    later = _msg(4, "P5", "这个人机怎么不毒舌了", event_type="question", event_id="ev_talk", msg_id="m_talk")
    return [request, reply, pat, pat_reply, later]


def test_derive_links() -> bool:
    links = derive_answer_links(_rows())
    by_req = {l.request_event_id: l for l in links}
    ok1 = set(by_req) == {"ev_req", "ev_pat"}
    link = by_req.get("ev_req")
    ok2 = (
        link is not None
        and link.reply_event_id == "ev_reply"
        and link.target_person_id == "wxid_seed01"
        and link.target_name == "Elaina"
        and link.target_mismatch is False
    )
    # 缺 parent / parent 对不上 → 不产出
    orphan = _msg(5, "鲸鲸", "@某人 我在", is_self=True, event_type="reply", event_id="ev_orphan")
    ok3 = derive_answer_links([orphan]) == []
    print(f"  {'✓' if ok1 and ok2 and ok3 else '✗'} 派生: keys={sorted(by_req)} link={link}")
    return bool(ok1 and ok2 and ok3)


def test_annotate_line() -> bool:
    rows = _rows()
    links = links_by_request(rows)
    request = rows[0]
    pat = rows[2]
    talk = rows[4]
    line_req = annotate_candidate_line("[13:23] Elaina: 帮我搜索…", request, links)
    line_pat = annotate_candidate_line("[13:24] （系统）P5 拍了拍 鲸鲸", pat, links)
    line_talk = annotate_candidate_line("[13:24] P5: 这个人机怎么不毒舌了", talk, links)
    ok = (
        line_req.startswith("[已答 13:25→@Elaina]")
        and line_pat == "[13:24] （系统）P5 拍了拍 鲸鲸"
        and line_talk == "[13:24] P5: 这个人机怎么不毒舌了"
    )
    print(f"  {'✓' if ok else '✗'} 标注: {line_req!r} | pat 跳过={line_pat == '[13:24] （系统）P5 拍了拍 鲸鲸'}")
    return bool(ok)


def test_augment_pairs() -> bool:
    rows = _rows()
    # 只有请求 → 补回她的答复
    only_req = [rows[0]]
    aug = augment_rows_with_links(only_req, rows)
    ok1 = [m.event_id for m in aug] == ["ev_req", "ev_reply"]
    # 只有答复 → 补回请求
    only_reply = [rows[1]]
    aug2 = augment_rows_with_links(only_reply, rows)
    ok2 = [m.event_id for m in aug2] == ["ev_req", "ev_reply"]
    # 已成对 → 不动
    both = [rows[0], rows[1]]
    aug3 = augment_rows_with_links(both, rows)
    ok3 = [m.event_id for m in aug3] == ["ev_req", "ev_reply"]
    # 时间窗外的答复不补
    far = _msg(-120, "鲸鲸", "@Elaina 早答过了", is_self=True, event_type="reply",
               event_id="ev_far", parent="m_req", recipients=("wxid_seed01",))
    aug4 = augment_rows_with_links([rows[0]], rows + [far], window_seconds=1800)
    ok4 = "ev_far" not in [m.event_id for m in aug4]
    ok = ok1 and ok2 and ok3 and ok4
    print(f"  {'✓' if ok else '✗'} 成对: 补答复={ok1} 补请求={ok2} 已对={ok3} 窗外={ok4}")
    return bool(ok)


def test_dedupe_prefers_reply_twin() -> bool:
    echo = _msg(2.0, "鲸鲸", "@Elaina 有，直接说几个能用的：Webfem…",
                is_self=True, event_type="question", event_id="ev_echo")
    reply = _msg(2.3, "鲸鲸", "@Elaina 有，直接说几个能用的：Webfem…",
                 is_self=True, event_type="reply", event_id="ev_reply",
                 parent="m_req", recipients=("wxid_seed01",))
    kept = _dedupe_self_echoes([echo, reply])
    ok = len(kept) == 1 and kept[0].event_id == "ev_reply"
    print(f"  {'✓' if ok else '✗'} 去重优先留 reply 分身: kept={[m.event_id for m in kept]}")
    return bool(ok)


def test_switch_off() -> bool:
    with _env(**{"YINYUE_ANSWER_LINKS": "0"}):
        ok = derive_answer_links(_rows()) == []
    with _env(**{"YINYUE_ANSWER_LINKS": None}):
        ok2 = len(derive_answer_links(_rows())) == 2
    print(f"  {'✓' if ok and ok2 else '✗'} 开关: off=空 tie= {ok} | 默认开={ok2}")
    return bool(ok and ok2)


class _env:
    def __init__(self, **values):
        self._values = values

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._values}
        for k, v in self._values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    tests = [
        test_derive_links,
        test_annotate_line,
        test_augment_pairs,
        test_dedupe_prefers_reply_twin,
        test_switch_off,
    ]
    results = []
    for test in tests:
        try:
            results.append((test.__name__, bool(test())))
        except Exception:  # pragma: no cover
            import traceback

            traceback.print_exc()
            results.append((test.__name__, False))
    print()
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    failed = [n for n, ok in results if not ok]
    print(f"answer-link checks: {len(results) - len(failed)}/{len(results)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
