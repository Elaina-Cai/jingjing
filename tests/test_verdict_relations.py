"""四档关系对照：same/finer/silent/loud（旧 agree/disagree 无信息量）。"""
from __future__ import annotations

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.verdict.matrix import ABSTAIN, ASK, DO, SAY, Decision  # noqa: E402
from core.verdict.reading import Reading  # noqa: E402
from core.verdict import shadow as S  # noqa: E402
from core.verdict import matrix as M  # noqa: E402

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name + " " + str(detail))


def rel(new, spoke=True):
    return S.compare_levels(old_would_reply=spoke, decision=Decision(new, "x"), chat_id="c1")["relation"]


def t_relations():
    check("旧说→说 = same", rel(SAY) == "same")
    check("旧说→问 = finer", rel(ASK) == "finer")
    check("旧说→办 = finer", rel(DO) == "finer")
    check("旧说→弃权 = silent", rel(ABSTAIN) == "silent")
    check("旧不说→说 = loud", rel(SAY, spoke=False) == "loud")
    check("旧不说→弃权 = same", rel(ABSTAIN, spoke=False) == "same")
    out = S.compare_levels(old_would_reply=True, decision=Decision(ASK, "missing_slot"), msg_id="m1", text="杭州明天会下雨吗")
    check("字段齐全", set(out) == {"old_level", "new_level", "relation"}, out)


def t_no_confidence_gate():
    src = pathlib.Path("core/verdict/matrix.py").read_text(encoding="utf-8")
    check("代码里已无 low_confidence", "low_confidence" not in src)
    check("代码里已不 import LOW_CONFIDENCE", "LOW_CONFIDENCE" not in src)
    low = Reading(quotes=["在吗"], slots={}, gaps=0, timing="", affect=0.0, confidence=0.1)
    check("低 confidence 不再导致 ask", M.decide(low).disposition == SAY, M.decide(low))


def t_reasons_verifiable():
    only = {"no_capability", "no_quote", "missing_slot", "needs_confirm", "direct", "comfort", "short"}
    got = set()
    r1 = Reading(quotes=[], slots={}, gaps=0, timing="", affect=0.0, confidence=0.9)
    got.add(M.decide(r1, capability=False).reason)
    got.add(M.decide(Reading(quotes=[], slots={}, gaps=0, timing="", affect=0, confidence=.9, wants=("do",))).reason if hasattr(Reading, "wants") else "no_quote")
    got.add(M.decide(Reading(quotes=["q"], slots={"who": ""}, gaps=1, timing="", affect=0, confidence=.9)).reason)
    got.add(M.decide(Reading(quotes=["q"], slots={}, gaps=0, timing="", affect=0, confidence=.9)).reason)
    check("理由都在可验证集合内", got <= only, got)


def main():
    for fn in (t_relations, t_no_confidence_gate, t_reasons_verifiable):
        print(fn.__name__)
        fn()
    print("RESULT %d failed / %d checks" % (len(FAILS), COUNT))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
