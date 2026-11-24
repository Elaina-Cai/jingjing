r"""六项能力自检：探不出来一律按「不过」，理由可读、顺序固定。

跑法：.venv_it\Scripts\python.exe -X utf8 -u tests/test_capability_probe.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui import capability as C  # noqa: E402
from core.execution.gui.lease import CapabilityMissing  # noqa: E402

FAILS: list[str] = []
COUNT = [0]


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL {name}  {detail}")


def all_ok(**overrides) -> dict:
    base = {item: (lambda **k: True) for item in C.ITEMS}
    base.update(overrides)
    return base


def raises(fn, exc_type):
    try:
        fn()
    except exc_type as exc:
        return exc
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


def main() -> int:
    print("[1] 六项全过：ok，没有失败项")
    report = C.probe(probes=all_ok())
    check("ok", report.ok)
    check("failures 空", report.failures() == [])
    check("reason 空", report.reason == "")
    check("one_line 形如 caps=111111 ok", report.one_line() == "caps=111111 ok", report.one_line())
    check("require 不抛", raises(lambda: C.require(report), Exception) is None)

    print("[2] 每一项单独不过：理由要准")
    cases = {
        "integrity": "integrity_too_low",
        "session": "session_locked",
        "dpi": "dpi_mismatch",
        "identity": "target_unknown",
        "writable": "composer_unwritable",
        "menu": "menu_unreadable",
    }
    for item, expected in cases.items():
        bad = C.probe(probes=all_ok(**{item: (lambda **k: False)}))
        check(f"{item} -> {expected}", not bad.ok and bad.reason == expected, bad.reason)
        exc = raises(lambda r=bad: C.require(r), CapabilityMissing)
        check(f"{item} require 抛 {expected}",
              isinstance(exc, CapabilityMissing) and exc.reason.startswith(expected),
              repr(exc))
        check(f"{item} 只报这一项", bad.failures() == [item], str(bad.failures()))

    print("[3] 探针抛异常 = 不过（fail closed）")
    def boom(**k):
        raise OSError("access denied")

    rep = C.probe(probes=all_ok(session=boom))
    check("异常算不过", not rep.session_active)
    check("detail 记 probe_error", rep.detail.get("session", "").startswith("probe_error:"),
          rep.detail.get("session", ""))
    check("reason 仍是 session_locked", rep.reason == "session_locked", rep.reason)

    print("[4] 多项不过：reason 取固定顺序的第一条（integrity 最优先）")
    multi = C.probe(probes=all_ok(menu=(lambda **k: False), integrity=(lambda **k: False)))
    check("先报 integrity", multi.reason == "integrity_too_low", multi.reason)
    check("failures 按固定顺序", multi.failures() == ["integrity", "menu"], str(multi.failures()))
    check("missing 同步", multi.missing() == ["integrity_too_low", "menu_unreadable"],
          str(multi.missing()))

    print("[5] 报告能一行进日志 / 能序列化")
    line = multi.one_line()
    check("one_line 带 blocked 理由", line == "caps=011110 blocked:integrity_too_low", line)
    payload = multi.as_dict()
    check("as_dict 可 json 序列化", isinstance(json.dumps(payload, ensure_ascii=False), str))
    check("as_dict 含六项与 detail",
          all(k in payload for k in C.ITEMS) and isinstance(payload["detail"], dict))

    print("[6] 缺探针 = 不过（不许静默当成没问题）")
    partial = {item: (lambda **k: True) for item in C.ITEMS}
    partial["dpi"] = None  # 显式声明「这个探针缺失」
    rep2 = C.probe(probes=partial)
    check("缺 dpi -> 不过", not rep2.dpi_ok and rep2.reason == "dpi_mismatch", rep2.reason)
    check("detail 记 probe_missing", rep2.detail.get("dpi") == "probe_missing",
          rep2.detail.get("dpi", ""))

    print("[7] 真机探针也在表里（不执行，只校验存在且可调用）")
    check("六个默认探针齐全", set(C.DEFAULT_PROBES) == set(C.ITEMS), str(sorted(C.DEFAULT_PROBES)))
    check("REASONS 与 ITEMS 对齐", set(C.REASONS) == set(C.ITEMS))
    check("完整性阈值是 Medium", C._INTEGRITY_MEDIUM == 0x2000)

    print("[8] require 的理由串可读（带上 detail）")
    exc = raises(lambda: C.require(C.probe(probes=all_ok(writable=boom))), CapabilityMissing)
    check("理由含 composer_unwritable", "composer_unwritable" in str(exc), str(exc))

    print()
    print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
    for line in FAILS:
        print("  -", line)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
