from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.jobs import ask as A
from core.speech.outbound import OutboundGateway

FAILS: list[str] = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if cond:
        print("  ok  " + name)
    else:
        FAILS.append(name)
        print("  FAIL " + name, detail)


def main() -> int:
    print("== 完成式守卫 ==")
    drop = OutboundGateway._drop_done_claims
    plain = "要不要我往群里发这一句？"
    check("没有完成式不动它", drop(plain) == plain)
    mixed = "已经发好了。还有别的吗？"
    out = drop(mixed)
    check("完成式被摘掉", not A.looks_like_done_claim(out) and "还有别的吗" in out)
    only = drop("批准收到，我去发。")
    check("全是完成式就换成实话", "没发出去" in only)
    check("空串原样", drop("") == "")
    check("多句只摘含完成式的那句", drop("先说已经发好了。要不要我发？").count("已经发好了") == 0)
    src = Path(OutboundGateway.__module__.replace(".", "/") + ".py")
    body = (ROOT / src).read_text(encoding="utf-8")
    check("挂在发送前", "_drop_done_claims" in body and body.index("self._drop_done_claims(") < body.index("owner_error = self._owner_account_error("))
    # 2026-10-23：摘完成式的实现搬到 core/speech/plan_ingest.py（路由侧原来另有两份实现）
    ingest = (ROOT / "core" / "speech" / "plan_ingest.py").read_text(encoding="utf-8")
    check("守卫失败不拦发送", "done_claim_guard_failed" in ingest)
    check("出口仍负责摘完成式", "plan_ingest.drop_done_claims(" in body)
    print(f"RESULT {len(FAILS)} failed / {COUNT} checks")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
