"""最小 pytest 替身：给 tests/ 里那些「按 pytest 风格写、却从未真跑过」的文件一个入口。

`.venv` 没装 pytest，`pyproject.toml` 也把 pytest 配成不收集 tests/ —— 所以带
`monkeypatch` 参数或没有 `main()` 的用例文件被 `python tests/x.py` 跑时，模块体只定义
函数就退出了，长期被算 PASS。本模块只干两件事：

1. `_MonkeyPatch`：够用的 monkeypatch 替身（setattr / delattr / setenv / delenv / undo）；
2. `run_all(globals())`：按定义顺序跑本模块里所有只接受 `monkeypatch` / `tmp_path`
   两个 fixture 的 `test_*` / `t_*` 函数（`tmp_path` 每条用例给一个新的临时目录）。

刻意不复刻 pytest 的 fixture / 参数化 / 近似匹配 —— 遇到要这些东西的用例，它就该被
改写成脚本，而不是被这层遮住。
"""
import inspect
import os
import shutil
from pathlib import Path

# 临时目录的唯一入口（见 tests/_tempdirs.py）：裸 ``mkdtemp()`` 在系统 TEMP 不可写时
# 会静默回落到仓库根。
from _tempdirs import make_db_dir


class _MonkeyPatch:
    def __init__(self) -> None:
        self._undo = []

    def setattr(self, target, name, value=None, raising=True):
        if isinstance(target, str):
            # pytest 的字符串形式（"a.b.C"）——这里不支持，直接炸，免得静默写错地方。
            raise NotImplementedError("_MonkeyPatch 只支持对象形式的 setattr")
        old = getattr(target, name, None)
        had = hasattr(target, name)
        self._undo.append(lambda: (setattr(target, name, old) if had else delattr(target, name)))
        setattr(target, name, value)
        return value

    def delattr(self, target, name, raising=True):
        old = getattr(target, name)
        self._undo.append(lambda: setattr(target, name, old))
        delattr(target, name)

    def setenv(self, name, value, prepend=None):
        old = os.environ.get(name)
        self._undo.append(lambda: (os.environ.pop(name, None) if old is None else os.environ.__setitem__(name, old)))
        os.environ[name] = str(value)

    def delenv(self, name, raising=True):
        old = os.environ.get(name)
        if old is not None:
            self._undo.append(lambda: os.environ.__setitem__(name, old))
        os.environ.pop(name, None)

    def undo(self):
        while self._undo:
            self._undo.pop()()


def _cases(ns):
    out = []
    for name, obj in list(ns.items()):
        if not (name.startswith("test_") or name.startswith("t_")):
            continue
        if not inspect.isfunction(obj) or obj.__module__ != ns.get("__name__"):
            continue
        params = [p for p in inspect.signature(obj).parameters.values()
                  if p.default is p.empty
                  and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        names = [p.name for p in params]
        if [n for n in names if n not in ("monkeypatch", "tmp_path")]:
            continue
        out.append((obj.__code__.co_firstlineno, name, obj, names))
    out.sort()
    return out


def run_all(ns):
    cases = _cases(ns)
    if not cases:
        print("没有可跑用例（%s）" % ns.get("__name__", "?"))
        return False
    failed = []
    for _, name, fn, wants in cases:
        mp = _MonkeyPatch() if "monkeypatch" in wants else None
        tmp = make_db_dir("mini_pytest_") if "tmp_path" in wants else None
        kwargs = {}
        if mp is not None:
            kwargs["monkeypatch"] = mp
        if tmp is not None:
            kwargs["tmp_path"] = tmp
        try:
            fn(**kwargs)
            print("ok   " + name)
        except BaseException as exc:  # noqa: BLE001 —— 这里是套件入口，必须把红原文带出来
            failed.append((name, exc))
            print("FAIL " + name + ": " + type(exc).__name__ + ": " + str(exc))
        finally:
            if mp is not None:
                try:
                    mp.undo()
                except BaseException:
                    pass
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
    print("%d/%d passed" % (len(cases) - len(failed), len(cases)))
    return not failed