"""E1 验收（U-02）：输出契约校验（三必填 + 三条件）。

跑法：python -X utf8 -u tests/test_verdict_contract.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.conversational import audit_verdict_contract  # noqa: E402
from core import features  # noqa: E402
from core.contracts import verdict as V  # noqa: E402

ENV = "YINYUE_FEATURE_VERDICT_CONTRACT"
FAILS: list[str] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    if cond:
        print(f"  ok    {name}")
        return
    print(f"  FAIL  {name}  {detail}")
    FAILS.append(name)


def base(**kw: object) -> dict:
    payload = {
        "decision": "respond",
        "deliver": [{"kind": "say", "text": "hi"}],
        "basis": ["w#3"],
        "reading": "他们在聊作业",
    }
    payload.update(kw)
    return payload


def errors_of(payload: dict) -> tuple[str, ...]:
    return V.validate_verdict(payload).errors


def main() -> int:
    os.environ.pop(ENV, None)

    check("开关已注册为三态", features.VERDICT_CONTRACT in features.TRI_STATE_FLAGS)
    check(
        "默认 shadow",
        features.mode(features.VERDICT_CONTRACT) == features.MODE_SHADOW,
        features.mode(features.VERDICT_CONTRACT),
    )

    ok = V.validate_verdict(base())
    check(
        "合法载荷",
        ok.ok and ok.decision == "respond" and ok.deliver_count == 1 and ok.basis_count == 1,
        ok.as_dict(),
    )
    check("缺 decision", errors_of({"deliver": [], "basis": []}) == (V.ERR_DECISION_MISSING,))
    check("decision 非法", errors_of(base(decision="defer")) == (V.ERR_DECISION_INVALID,))
    check("respond 无 deliver", errors_of(base(deliver=[])) == (V.ERR_DELIVER_REQUIRED,))
    check(
        "silent 带 deliver",
        errors_of(base(decision="silent")) == (V.ERR_DELIVER_MUST_BE_EMPTY,),
    )
    check(
        "deliver 超 2 条",
        V.ERR_DELIVER_TOO_MANY in errors_of(base(deliver=[{"kind": "say"}] * 3)),
    )
    check(
        "两条 say",
        V.ERR_DELIVER_SAY_MORE_THAN_ONE in errors_of(base(deliver=[{"kind": "say"}, {"kind": "say"}])),
    )
    check(
        "deliver 项缺 kind",
        V.ERR_DELIVER_ITEM_INVALID in errors_of(base(deliver=[{"text": "x"}])),
    )
    check("缺 basis 键", V.ERR_BASIS_MISSING in errors_of(base(basis=None)))
    check("开口但 basis 空", V.ERR_BASIS_EMPTY_ON_SPEAK in errors_of(base(basis=[])))
    check("开口但缺 reading", V.ERR_READING_MISSING in errors_of(base(reading="")))
    check(
        "memory 超 3 条",
        V.ERR_MEMORY_TOO_MANY in errors_of(base(memory=[{"kind": "k", "key": "a", "value": "v"}] * 4)),
    )
    check(
        "memory 项不完整",
        V.ERR_MEMORY_ITEM_INVALID in errors_of(base(memory=[{"kind": "k", "value": "v"}])),
    )
    check(
        "四段齐全的完整载荷",
        V.validate_verdict(
            base(
                memory=[{"kind": "fact", "key": "作业", "value": "缺第三章", "basis": ["w#3"]}],
            )
        ).ok,
    )

    mapped = V.payload_from_extra({"turn_decision": "respond", "basis": ["w#1"]})
    check(
        "payload_from_extra 只做只读映射",
        mapped == {"decision": "respond", "basis": ["w#1"]}
        and V.ERR_DELIVER_MISSING in V.validate_verdict(mapped).errors,
        mapped,
    )
    check("空载荷不校验", audit_verdict_contract(payload=None) is None)

    os.environ[ENV] = "off"
    check("off 档直接返回 None", audit_verdict_contract(payload=base(deliver=[])) is None)

    os.environ[ENV] = "shadow"
    shadow = audit_verdict_contract(payload=base(deliver=[]))
    check(
        "shadow 只记不拒",
        shadow is not None and not shadow.ok and V.ERR_DELIVER_REQUIRED in shadow.errors,
        None if shadow is None else shadow.as_dict(),
    )

    os.environ[ENV] = "on"
    raised = False
    try:
        audit_verdict_contract(payload=base(deliver=[]))
    except V.VerdictContractError as exc:
        raised = V.ERR_DELIVER_REQUIRED in exc.result.errors
    check("on 档不合法即拒", raised)
    ok_on = audit_verdict_contract(payload=base())
    check("on 档合法载荷放行", ok_on is not None and ok_on.ok)

    os.environ[ENV] = "on"
    check("on 档空载荷仍不校验", audit_verdict_contract(payload={}) is None)
    os.environ.pop(ENV, None)

    print(f"RESULT {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
