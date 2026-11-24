# -*- coding: utf-8 -*-
"""看门狗自愈链路的护栏（2026-11-02）。

守四件实锤过的事故：
1. 看门线程是一次性的 —— 按过一次 all.stop 就 `return` 退场，重启流程把这次
   触发消耗掉之后，all.stop 再出现就没人认（00:37 与 11-02 12:13 两次重启假成功）；
2. `_log` 先 print 后落盘 —— stdout 是没人读的管道时，print 会把监管器冻成
   "占着互斥体、对一切无反应"的僵尸；
3. 看门狗探测端口写死 8972（引擎实际在 39871），就算注册了也是瞎的；
4. 监管器挂死后互斥体不释放，普通拉起永远被弹开 —— 看门狗要认心跳、能杀树。
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SUP = os.path.join(ROOT, "scripts", "supervisor.py")
WD = os.path.join(ROOT, "scripts", "watchdog_yinyue.py")


def _src(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _fn_block(src: str, header: str, end_headers: tuple) -> str:
    i = src.index(header)
    ends = [src.index(h, i + 10) for h in end_headers if h in src[i + 10:]]
    return src[i:min(ends)] if ends else src[i:]


def test_watch_all_stop_is_resident():
    """按下哨兵后不许退场 —— 要继续盯到进程退出（重启流程会清哨兵再重来）。"""
    src = _src(SUP)
    body = _fn_block(src, "def _watch_all_stop", ("def _heartbeat_loop", "SUPERVISOR_LOCK ="))
    press = body.index('_press_sentinels("all.stop")')
    tail = body[press:]
    assert "return" not in tail.split("while _all_stop_requested")[0], "按下哨兵后不许 return"
    assert "while _all_stop_requested() and not stop_evt.is_set()" in tail, "要等总闸消失再继续盯"


def test_log_writes_file_before_stdout():
    """_log 的可信落点是 supervisor.out，必须先落盘；stdout 只是锦上添花。"""
    src = _src(SUP)
    body = _fn_block(src, "def _log", ("def _pid_alive", "def _all_stop_requested"))
    assert body.index('os.path.join(LOGS, "supervisor.out")') < body.index("print(line"), \
        "文件写必须发生在 print 之前"
    assert "out = sys.stdout" in body and "if out is not None" in body, "stdout 要判空"


def test_stdout_is_defused():
    src = _src(SUP)
    assert "def _defuse_stdout" in src
    i = src.index("def main()")
    assert "_defuse_stdout()" in src[i:i + 2000], "main() 开头要排 stdout 的险"


def test_heartbeat_thread_started():
    src = _src(SUP)
    assert "supervisor-heartbeat" in src, "心跳线程要起"
    assert "supervisor_heartbeat.json" in src


def test_no_proxy_for_children():
    src = _src(SUP)
    i = src.index("def main()")
    head = src[i:i + 2500]
    assert "NO_PROXY" in head and "127.0.0.1" in head, "main() 开头要给子进程设 NO_PROXY"


def test_watchdog_probes_the_real_port():
    """探测端口必须跟着引擎走（8972 是老架构的遗物，探它永远瞎）。"""
    import scripts.watchdog_yinyue as W

    assert W.SERVICE_PORT == 39871, W.SERVICE_PORT
    src = _src(WD)
    assert "8972" not in src, "8972 残留"


def test_supervisor_hung_verdicts():
    """新鲜=没挂；过期=挂（带 pid）；没文件=交给正常拉起路径。"""
    import scripts.watchdog_yinyue as W

    d = tempfile.mkdtemp(prefix="wd_probe_")
    now = time.time()
    fresh = os.path.join(d, "fresh.json")
    with open(fresh, "w", encoding="utf-8") as fh:
        json.dump({"pid": 4321, "ts": now - 10}, fh)
    hung, why = W.supervisor_hung(path=fresh, now=now)
    assert not hung and "新鲜" in why, why

    stale = os.path.join(d, "stale.json")
    with open(stale, "w", encoding="utf-8") as fh:
        json.dump({"pid": 4321, "ts": now - 999}, fh)
    hung, why = W.supervisor_hung(path=stale, now=now)
    assert hung and "4321" in why, why

    missing = os.path.join(d, "nope.json")
    hung, why = W.supervisor_hung(path=missing, now=now)
    assert not hung and "无心跳" in why, why


def test_kill_is_confined_to_taskkill():
    """杀树只许用 taskkill /T /F，不许出现更宽的清理手段。"""
    src = _src(WD)
    body = _fn_block(src, "def kill_supervisor_tree", ("def run_task_start",))
    assert '["taskkill", "/PID", str(pid), "/T", "/F"]' in body


def main() -> bool:
    names = sorted(n for n in globals() if n.startswith("test_"))
    fails = []
    for n in names:
        fn = globals()[n]
        if not callable(fn):
            continue
        try:
            fn()
            print("  ok   %s" % n)
        except Exception as exc:  # noqa: BLE001 - 汇总用，单个用例炸了要继续跑完
            fails.append(n)
            print("  FAIL %s  %s" % (n, repr(exc)[:200]))
    print("RESULT %d failed / %d" % (len(fails), len(names)))
    return not fails


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
