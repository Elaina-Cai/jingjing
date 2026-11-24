"""监管器启动面的回归（2026-10-28 事故）。

事故：UI-066 把 `_scheduler_alive(max_age_s)` 改成无参（节拍由生产者写进心跳），
`scripts/supervisor.py` 的调用点还传着 300.0 → 启动即 TypeError → 没人拉调度器与引擎，
她整条链停摆约 20 分钟。四检全绿也拦不住，因为**没有任何测试会 import 监管器**。
"""
import ast
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SUP = os.path.join(ROOT, "scripts", "supervisor.py")


def _load():
    spec = importlib.util.spec_from_file_location("_sup_probe", SUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_zero_arg_helpers_are_not_called_with_args():
    """形状规则：本文件里定义的无参函数，任何调用点都不许传参。"""
    tree = ast.parse(open(SUP, encoding="utf-8").read())
    zero = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = n.args
            if not (args.posonlyargs or args.args or args.kwonlyargs or args.vararg or args.kwarg):
                zero.add(n.name)
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in zero:
            if n.args or n.keywords:
                bad.append("%s() 在 %d 行被传参调用" % (n.func.id, n.lineno))
    assert not bad, bad


def test_module_imports_and_main_is_guarded():
    """导入监管器不许有副作用（main 必须在 __main__ 保护里）。"""
    mod = _load()
    assert hasattr(mod, "main")
    src = open(SUP, encoding="utf-8").read()
    tail = src.rsplit("if __name__", 1)
    assert len(tail) == 2 and "main()" in tail[1], "main 没有被 __main__ 保护"


def test_clear_stale_lock_does_not_raise():
    """启动第一步：清陈旧锁不许抛（那一步抛了整条链就没人拉）。"""
    mod = _load()
    mod._clear_stale_lock()


def test_heartbeat_comes_from_liveness():
    """消费者不许自己嗅心跳格式：路径与判死都走 core.infra.liveness。"""
    src = open(SUP, encoding="utf-8").read()
    assert "liveness" in src
    assert "loop_heartbeat.json" not in src, "监管器又自己去拼心跳文件名了"


if __name__ == "__main__":
    test_zero_arg_helpers_are_not_called_with_args()
    test_module_imports_and_main_is_guarded()
    test_clear_stale_lock_does_not_raise()
    test_heartbeat_comes_from_liveness()
    print("SUPERVISOR_BOOT_OK")
