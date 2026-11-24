"""U-21 backend 注册表：换手不换工具，且不认识的/不会这个动词的当场失败。

钉住的边界：

- `resolve("")` 用调用点给的默认值（读屏默认 `cdp`、控件树默认 `uia`）；
- 不认识的 backend 名 = `unsupported`，**绝不回退**默认那只手；
- `GUI_BACKENDS=off` 只放行 `cdp` / `uia`，新登记的手必须 `on` 才选得中（`blocked`）；
- 动词不在说明书里 = `unsupported`（`uia` 今天不做点击）；
- `reader()` 只构造不连接（构造 `CdpReader` 不发任何请求）。

跑法：.venv_it\\Scripts\\python.exe -X utf8 -u tests/test_gui_backends.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution.gui import backends as B  # noqa: E402
from core.execution.gui.base import GuiError  # noqa: E402

COUNT = [0]
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT[0] += 1
    if cond:
        print(f"  ok   {name}")
        return
    FAILS.append(f"{name} {detail}".strip())
    print(f"  FAIL {name} {detail}".strip())


def with_mode(mode: str):
    prev = B._mode
    B._mode = lambda: mode
    return prev


def code_of(fn) -> str:
    try:
        fn()
    except GuiError as exc:
        return exc.code
    return "no-error"


class _Fake:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def snapshot(self, target: str = "", *, max_chars: int = 4000):
        self.calls.append(f"snapshot:{target}")
        return {"ok": True}


def t_registry() -> None:
    print("[t_registry]")
    names = [i.name for i in B.infos()]
    check("登记了 cdp 与 uia", names == ["cdp", "uia"], str(names))
    cdp = B.describe("cdp")
    check("cdp 五个动词都会", set(cdp.verbs) == set(B.VERBS), str(sorted(cdp.verbs)))
    uia = B.describe("uia")
    check("uia 今天只会读", set(uia.verbs) == {"state"}, str(sorted(uia.verbs)))
    check("describe() 空名给列表", len(B.describe()) == 2)
    check("describe 未知给 None", B.describe("nope") is None)
    check("supports 正向", B.supports("cdp", "click") is True)
    check("supports 反向", B.supports("uia", "click") is False)


def t_resolve() -> None:
    print("[t_resolve]")
    prev = with_mode("off")
    try:
        check("空名走默认 cdp", B.resolve("", verb="snapshot", default="cdp") == "cdp")
        check("空名走默认 uia", B.resolve("", verb="state", default="uia") == "uia")
        check("显式 cdp", B.resolve("cdp", verb="click") == "cdp")
        check("未知名字是 unsupported", code_of(lambda: B.resolve("vision")) == "unsupported")
        check("uia 不做点击", code_of(lambda: B.resolve("uia", verb="click")) == "unsupported")
        check("没有默认值也不许空", code_of(lambda: B.resolve("", default="")) == "unsupported")
    finally:
        B._mode = prev

    # 新登记的手在 off 档必须被挡住
    info = B.BackendInfo(name="vision", verbs=frozenset({"click"}))
    B.register(info, lambda: _Fake())
    prev = with_mode("off")
    try:
        check("off 档挡新 backend", code_of(lambda: B.resolve("vision", verb="click")) == "blocked")
    finally:
        B._mode = prev
    prev = with_mode("on")
    try:
        check("on 档放行新 backend", B.resolve("vision", verb="click") == "vision")
        check("on 档仍查动词", code_of(lambda: B.resolve("vision", verb="snapshot")) == "unsupported")
    finally:
        B._mode = prev
    B.unregister("vision")
    check("注销后回到 unsupported", code_of(lambda: B.resolve("vision")) == "unsupported")
    check("注销后说明书也没了", B.describe("vision") is None)


def t_reader() -> None:
    print("[t_reader]")
    cdp = B.reader("", verb="snapshot", default="cdp")
    check("cdp reader 类型", type(cdp).__name__ == "CdpReader", type(cdp).__name__)
    uia = B.reader("", verb="state", default="uia")
    check("uia reader 类型", type(uia).__name__ == "UiaReader", type(uia).__name__)
    check("uia reader 带了时间戳", bool(getattr(uia, "_taken_at", "")), str(getattr(uia, "_taken_at", "")))
    fake = _Fake()
    B.register(B.BackendInfo(name="fake", verbs=frozenset({"snapshot"})), lambda: fake)
    prev = with_mode("on")
    try:
        got = B.reader("fake", verb="snapshot")
        check("reader 用登记的工厂", got is fake)
    finally:
        B._mode = prev
        B.unregister("fake")


def t_handlers_wired() -> None:
    print("[t_handlers_wired]")
    src = pathlib.Path("core/tools/builtin.py").read_text(encoding="utf-8")
    check("五个 GUI 工具都走注册表", src.count("backends.reader(") == 5, str(src.count("backends.reader(")))
    check("点击默认 cdp", 'verb="click", default="cdp"' in src)
    check("控件树默认 uia", 'verb="state", default="uia"' in src)
    check("不再直接 new CdpReader", "CdpReader()" not in src)
    check("不再直接 new UiaReader", "UiaReader(" not in src)
    bsrc = pathlib.Path("core/execution/gui/backends.py").read_text(encoding="utf-8")
    check("registry 不 import 说话层", "core.speech" not in bsrc)
    check("registry 不碰网络", "http" not in bsrc.replace("https://", "").lower() or "requests" not in bsrc)
    check("有开关读数点", "features.GUI_BACKENDS" in bsrc)


def t_no_silent_fallback() -> None:
    print("[t_no_silent_fallback]")
    prev = with_mode("off")
    try:
        bad = code_of(lambda: B.reader("uia", verb="click", default="cdp"))
        check("uia 点击不会回退 cdp", bad == "unsupported", bad)
    finally:
        B._mode = prev
    src = pathlib.Path("core/execution/gui/backends.py").read_text(encoding="utf-8")
    check("没有裸 except 回默认", "except Exception:" not in src)


def main() -> int:
    for fn in (t_registry, t_resolve, t_reader, t_handlers_wired, t_no_silent_fallback):
        fn()
    print()
    if FAILS:
        print(f"RESULT {len(FAILS)} failed / {COUNT[0]} checks")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"RESULT 0 failed / {COUNT[0]} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
