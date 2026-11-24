"""发出去之后的补偿：撤回窗口与更正消息（阶段 7 那一半）。

钉住四件事：
1. 撤回窗口只认回执里的时间，不认「我记得是刚才」；
2. 没有 `ref`（不知道在更正哪条）一律 fail closed；
3. 更正消息必须带原话片段，且原话太长要截断；
4. 这套东西**不碰库、不碰窗口**（纯函数，静态断言）。
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech import correction as cor  # noqa: E402


def _receipt(**kw):
    base = {
        "ref": "wechat#106",
        "target": "银行月的测试群",
        "at": "2026-10-21 19:44:00",
        "detail": "鲸鲸自检 A/B/C 19:44（一次，不等回复）",
        "status": "sent",
    }
    base.update(kw)
    return base


def main() -> int:
    failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failed
        if cond:
            print(f"PASS {name}")
            return
        failed += 1
        print(f"FAIL {name} :: {detail}")

    # 1) correction_text
    check("更正带前缀", cor.correction_text("明早九点开会", "改到十点").startswith(cor.CORRECTION_PREFIX))
    check("更正含原话与改口",
          cor.correction_text("明早九点开会", "改到十点") == "更正：明早九点开会 → 改到十点",
          cor.correction_text("明早九点开会", "改到十点"))
    check("空原话也能更正", cor.correction_text("", "改到十点") == "更正：改到十点")
    check("空改口返回空串", cor.correction_text("原话", "  ") == "", repr(cor.correction_text("原话", " ")))
    check("超长改口返回空串", cor.correction_text("原话", "改" * (cor.FIX_MAX_CHARS + 1)) == "")
    long_original = "一" * 60
    got = cor.correction_text(long_original, "改一下")
    check("长原话被截断", got.count("…") == 1 and len(got) < 60, got)
    check("空白折叠", "  " not in cor.correction_text("原   话", "改  口"), cor.correction_text("原   话", "改  口"))

    # 2) can_revoke
    at = datetime(2026, 10, 21, 19, 44, 0)
    ok, reason = cor.can_revoke(_receipt(at="2026-10-21 19:44:00"), now=at + timedelta(seconds=30))
    check("窗口内可撤回", ok is True and reason == "", f"{ok}/{reason}")
    ok, reason = cor.can_revoke(_receipt(at="2026-10-21 19:44:00"), now=at + timedelta(seconds=121))
    check("过期不可撤回", ok is False and reason == "expired", f"{ok}/{reason}")
    ok, reason = cor.can_revoke(_receipt(ref=""), now=at)
    check("没有 ref 不可撤回", ok is False and reason == cor.REASON_NO_REF, reason)
    ok, reason = cor.can_revoke(_receipt(at=""), now=at)
    check("时间读不出不可撤回", ok is False and reason == cor.REASON_NO_TIME, reason)
    ok, reason = cor.can_revoke(_receipt(at="不是时间"), now=at)
    check("时间格式错也不可撤回", ok is False and reason == cor.REASON_NO_TIME, reason)
    ok, _ = cor.can_revoke(_receipt(at=at), now=at)
    check("datetime 对象也认", ok is True)
    ok, _ = cor.can_revoke(_receipt(at="2026-10-21T19:44:00"), now=at + timedelta(seconds=10))
    check("ISO 分隔符也认", ok is True)
    ok, _ = cor.can_revoke(_receipt(at="2026-10-21 19:44:00"), now=at + timedelta(seconds=1), window_s=0)
    check("window_s=0 一秒后就过期", ok is False)

    # 3) plan_correction
    plan = cor.plan_correction(_receipt(at="2026-10-21 19:44:00"), "改到十点", now=at + timedelta(seconds=10))
    check("计划是 Correction", isinstance(plan, cor.Correction), type(plan).__name__)
    check("计划带 ref", plan.ref == "wechat#106", plan.ref)
    check("计划带 target", plan.target == "银行月的测试群", plan.target)
    check("窗口内 mode=revoke", plan.mode == "revoke", plan.mode)
    plan2 = cor.plan_correction(_receipt(at="2026-10-21 19:44:00"), "改到十点", now=at + timedelta(minutes=10))
    check("过期 mode=correct", plan2.mode == "correct", plan2.mode)
    check("计划带原话片段", plan2.original_excerpt.startswith("鲸鲸自检"), plan2.original_excerpt)
    check("计划正文含前缀", plan2.text.startswith(cor.CORRECTION_PREFIX), plan2.text)
    check("as_dict 五键", set(plan2.as_dict()) == {"ref", "target", "text", "excerpt", "mode"}, str(plan2.as_dict()))
    check("没有 ref 不出计划", cor.plan_correction(_receipt(ref=""), "改", now=at) is None)
    check("没有 target 不出计划", cor.plan_correction(_receipt(target=""), "改", now=at) is None)
    check("空改口不出计划", cor.plan_correction(_receipt(), "  ", now=at) is None)
    check("超长改口不出计划",
          cor.plan_correction(_receipt(), "改" * (cor.FIX_MAX_CHARS + 1), now=at) is None)

    # 4) 静态：这一层是纯函数
    src = io.open(ROOT / "core/speech/correction.py", encoding="utf-8").read()
    for banned in ("import sqlite3", "import sqlalchemy", "wx_human", "wx_chat", "press_enter",
                   "core.speech.rhythm", "core.speech.outbound"):
        check(f"不碰 {banned}", banned not in src, banned)
    check("常量 2 分钟", cor.REVOKE_WINDOW_S == 120, str(cor.REVOKE_WINDOW_S))

    print(f"RESULT {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
