"""yinyue 监管器：唯一有权拉起主程序（run.py scheduler）的进程。

2026-10-26 之前有好几套东西抢着拉起 scheduler（本文件、桌面启动器 yinyue_launcher.py、
watchdog_yinyue.py、autostart.bat 的 bat 级循环），谁也不服谁；当天 13:48 之后整晚没人
同步世界，引擎就对着五个小时前的旧视界说话。现在口径统一：

    谁想拉起主程序 → spawn scripts/supervisor.py，自己**不**spawn run.py。

两个哨兵（都在 logs/ 下）：

    watchdog.stop   外部"别再拉起我"的开关（autostart.bat / 启动器认这个）
    all.stop        总闸。写它就等于"全关"：监管器自己退出，并替用户按下
                    scheduler.stop 和 loop.stop，把主程序和 AI 引擎一起停掉
                    （2026-10-26 用户的原话："要么全开要么全关"）。

所以任何"启动"入口在拉起本文件之前，必须先把 all.stop 删掉。
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS = os.path.join(ROOT, "logs")
PY = sys.executable
sys.path.insert(0, ROOT)
from core.infra import liveness  # noqa: E402 - 心跳与判死只有一处实现
from core.infra.envflags import flag

RESTART_CODE = 42
MUTEX_NAME = "Global\\yinyue_supervisor"
MAX_BACKOFF_S = 60.0
CREATE_NO_WINDOW = 0x08000000

ALL_STOP = os.path.join(LOGS, "all.stop")
WATCHDOG_STOP = os.path.join(LOGS, "watchdog.stop")
_STOP_SENTINELS = ("scheduler.stop", "loop.stop")

def _acquire_mutex() -> bool:
    """单实例：命名互斥体。进程死即释放，不会像文件锁那样残留。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    h = k32.CreateMutexW(None, False, MUTEX_NAME)
    if not h:
        return False
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return False
    globals()["_MUTEX_HANDLE"] = h  # 持有到进程结束
    return True

def _clear_stale_lock() -> None:
    """端口没在听却留着 scheduler.lock = 上一个进程被强杀留下的僵尸锁。

    不清它，新进程会立刻 'scheduler_already_running' 自杀（2026-10-24 就卡在这）。
    """
    lock = os.path.join(LOGS, "scheduler.lock")
    if _scheduler_alive():
        return
    if os.path.exists(lock):
        try:
            os.remove(lock)
            _log("清掉僵尸锁 logs/scheduler.lock")
        except OSError as exc:
            _log("清锁失败：%r" % (exc,))

_STDOUT_DEAD = False  # stdout 管道死过一次就不再写（写文件那条路永远照走）


def _log(msg: str) -> None:
    global _STDOUT_DEAD
    line = "[supervisor] " + msg
    # 2026-11-02 修：原来是先 print 再写文件 —— stdout 若是"被重定向了又没人读"
    # 的管道（启动器/计划任务常见），print(flush=True) 会在缓冲区满后**永远阻塞**，
    # 整个监管器（含看门线程）一起冻死，表现成"占着互斥体、对 all.stop 无反应"
    # 的僵尸（2026-11-01 深夜实锤过一次）。现在文件是唯一可信落点，先落盘；
    # stdout 属于锦上添花，而且启动时就被 _defuse_stdout 排了险。
    try:
        os.makedirs(LOGS, exist_ok=True)
        with open(os.path.join(LOGS, "supervisor.out"), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass
    out = sys.stdout
    if out is not None and not _STDOUT_DEAD:
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001 - 控制台没了不该把监管器带走
            # 真处理：标记管道已死，后续 _log 不再往里写（写文件那条路永远照走）。
            _STDOUT_DEAD = True

def _pid_alive(pid) -> bool:
    """这个 pid 还在吗（实现在 liveness，别在这里再写第二份）。"""
    return liveness.pid_alive(pid)


def _take_over(sched_alive: bool, engine_state: str) -> bool:
    """要不要接管。**只要引擎不是 alive 就接管**：主程序活着不等于她能说话。

    2026-10-30：原来只看 `_scheduler_alive()` 就退场，结果引擎死了、主程序还活着时，
    监管器自己走了，没有人再把她拉起来（日志原文：「主程序心跳还在，说明已有实例在跑，
    监管器退出」）。另一条边界由互斥体保证：真有两个监管器时后到的那个在
    `_acquire_mutex()` 就退了，所以这里"接管"不会造成两个监管器。
    """
    return (not sched_alive) or engine_state != "alive"


def _scheduler_alive() -> bool:
    """主程序活着吗 = 心跳够新且 pid 还在。秒数不在这里数：节拍由生产者写进心跳。"""
    return liveness.alive("scheduler")


def _check_engine() -> None:
    """引擎卡死了吗，卡死就 kill（让它被 scheduler 重启）。

    判据全部来自 `core/infra/liveness.py`：`now - at > beat_s * GRACE`，
    beat_s 由引擎自己写进心跳。**这里不许出现任何写死的秒数。**
    心跳不合契约（unknown）也不判死：契约没被满足时**不猜**，只记一条响的日志。
    """
    st = liveness.state("engine")
    if st != "stale":
        # alive = 没事；gone = 没有进程可杀；unknown = 心跳不合契约，**不猜**。
        # 只有 unknown 需要喊一声，否则契约被违反时没人会知道（升级期忘记重启就是这样）。
        if st == "unknown":
            _log("引擎心跳不符合契约（%s），本次不判死，等它自己写合契约的心跳"
                 % "、".join(liveness.contract("engine")))
        return
    why = liveness.why("engine")
    pid = liveness.read("engine").get("pid")
    try:
        import psutil
        psutil.Process(int(pid)).kill()
    except Exception as exc:  # noqa: BLE001
        _log("引擎卡死（%s）但 kill pid=%s 失败：%r" % (why, pid, exc))
        return
    _log("引擎卡死（%s），已 kill pid=%s，交给 scheduler 重启" % (why, pid))


def _reap_orphan_engines(sched_pid: int, dry: bool = False) -> list:
    """2026-10-27：kill scheduler 会留下「孤儿引擎」。

    父进程（scheduler）死了，可它自己还在跑拍；而且它握着 run.py 的
    single-instance 锁（命名互斥体 + 127.0.0.1:39871），于是新拉起的引擎
    只能打印「已有引擎在跑」然后退出——监管器却完全看不见那个孤儿。
    判据：祖先链里没有本次拉起的 scheduler(pid=sched_pid) 的，就是孤儿。
    """
    killed = []
    # 留个后门：手工起引擎调试时设 YINYUE_NO_REAP=1，别让监管器把它当孤儿清掉
    if flag("YINYUE_NO_REAP"):
        return killed
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return killed
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = proc.info.get("cmdline") or []
        except Exception:  # noqa: BLE001
            continue
        if not any("loop.run" in str(x) for x in cl):
            continue
        if not any(str(x) == "-m" for x in cl):
            continue
        pid = proc.info.get("pid")
        if not pid or pid == os.getpid():
            continue
        orphan = False
        try:
            anc = {a.pid for a in proc.parents()}
            orphan = sched_pid not in anc
        except Exception:  # noqa: BLE001 - 父进程已经没了 = 典型孤儿
            orphan = True
        if not orphan:
            continue
        killed.append(pid)
        if dry:
            continue
        try:
            proc.kill()
        except Exception as exc:  # noqa: BLE001
            _log("清孤儿引擎 pid=%s 失败：%r" % (pid, exc))
    if killed and not dry:
        _log("清掉孤儿引擎 pid=%s（不在 sched=%s 这条链上）"
             % (",".join(str(x) for x in killed), sched_pid))
        _el = _exitlog()
        if _el:
            _el.record("engine", why="orphan_cleaned", pid=pid,
                       detail="父进程已死，监管器清掉")
    return killed




def _exitlog():
    """退出原因账（2026-11-05，主人「要」）：把「谁退了、什么码」写成一行 JSON。

    账本本身坏了不许影响监管（所以这里 try 住、只 _log 一句）；ROOT 不在 sys.path 时
    先插进去 —— 计划任务/双击启动时的 sys.path 里没有仓库根。
    """
    try:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from core.infra import exitlog
        return exitlog
    except Exception as exc:  # noqa: BLE001 - 账本坏了也不许影响监管
        _log("退出账本不可用：%r" % (exc,))
        return None

def _all_stop_requested() -> bool:
    return os.path.exists(ALL_STOP)

def _press_sentinels(why: str) -> None:
    """总闸被按下时，替用户把"自己停"的哨兵按下去（主程序/引擎各自认自己的）。"""
    for name in _STOP_SENTINELS:
        try:
            os.makedirs(LOGS, exist_ok=True)
            with open(os.path.join(LOGS, name), "w", encoding="utf-8") as fh:
                fh.write(why)
        except OSError:
            pass

def _watch_all_stop(stop_evt: threading.Event) -> None:
    """总闸是给人按的，不能等她下拍才生效：两秒看一次，看到就把自停哨兵按下去。

    2026-11-02 修：原来按下哨兵后就 `return` —— 看门线程是**一次性**的。重启流程
    （重启脚本：写 all.stop → 等退 → 清哨兵 → 起新的）会把这次触发消耗掉，此后
    all.stop 再出现就没人认了（00:37 与 11-02 12:13 两次重启假成功都是这么来的）。
    现在改为常驻：总闸消失（重启流程清掉哨兵）就继续盯，直到进程退出。
    """
    while not stop_evt.is_set():
        if _all_stop_requested():
            _press_sentinels("all.stop")
            while _all_stop_requested() and not stop_evt.is_set():
                stop_evt.wait(2.0)
            continue
        stop_evt.wait(2.0)

SUPERVISOR_LOCK = os.path.join(LOGS, "supervisor.lock")
#: 监管器心跳（给 scripts/watchdog_yinyue.py 判"监管器挂死"用：超过
#: SUPERVISOR_STALE_SECONDS 没动 = 进程冻住了，杀树重拉，否则互斥体会把
#: 一切拉起尝试弹开，谁也救不回来）。
HEARTBEAT_FILE = os.path.join(LOGS, "supervisor_heartbeat.json")
SUPERVISOR_STALE_SECONDS = 120.0


def _defuse_stdout() -> None:
    """stdout 若是"被重定向了又没人读"的管道，print 会把整个进程冻死。

    监管器的可信落点是 supervisor.out（见 _log），console 只是锦上添花：
    非 tty 的 stdout/stderr 一律扔 devnull，从根上拆掉这类冻结。
    """
    try:
        if sys.stdout is not None and not sys.stdout.isatty():
            sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        if sys.stderr is not None and not sys.stderr.isatty():
            sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    except Exception as exc:  # noqa: BLE001 - 排险失败就维持原样，别把启动搞挂
        _log("stdout 排险失败（维持原样）：%r" % (exc,))


def _heartbeat_loop(stop_evt: threading.Event) -> None:
    """每 5 秒摸一次心跳文件；进程一旦冻住（任何原因），心跳自然过期。"""
    while not stop_evt.is_set():
        try:
            tmp = "%s.%d.tmp" % (HEARTBEAT_FILE, os.getpid())  # 唯一名：jsonio 门禁禁固定 .tmp
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "ts": time.time()}))
            os.replace(tmp, HEARTBEAT_FILE)
        except OSError as exc:
            _log("心跳写失败（下轮再试）：%r" % (exc,))
        stop_evt.wait(5.0)


def _write_lock() -> None:
    try:
        with open(SUPERVISOR_LOCK, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError as exc:
        _log("写 logs/supervisor.lock 失败：%r" % (exc,))


def _drop_lock() -> None:
    try:
        os.remove(SUPERVISOR_LOCK)
    except OSError:
        pass


def _parent_cmdline() -> str:
    """谁把我拉起来的（2026-10-31：日志里 86 条「已有监管器在跑」查不出源头）。"""
    try:
        import psutil
        p = psutil.Process(os.getpid()).parent()
        if p is None:
            return "父进程已退出"
        try:
            cmd = " ".join(p.cmdline())
        except Exception:  # noqa: BLE001
            cmd = p.name()
        return "pid=%s %s" % (p.pid, cmd[:160])
    except Exception as exc:  # noqa: BLE001
        return "（取不到父进程：%r）" % (exc,)


def main() -> int:
    # 2026-11-06（B）：进 main 的第一件事就是记一条「我起来了」。
    # 必须放在前面：下面的全关/已有一份/心跳新鲜三条早退分支都会 return，
    # 放在函数末尾等于「只有在监管器正常退出时才记」—— 那正是我们最不需要它的时刻。
    try:
        _el0 = _exitlog()
        if _el0 is not None:
            _el0.record_start("supervisor")
    except Exception as _sexc:  # noqa: BLE001 - 记不下也不许拦住监管
        _log("exitlog.record_start 失败：%r" % (_sexc,))
    # 2026-11-02：本机回环不许走代理 —— 会话里若带着 HTTP(S)_PROXY，连 127.0.0.1
    # 的请求都会被代理拦成 502，假象能骗过一整轮人肉排查。子进程全部继承这里的环境。
    for _k in ("NO_PROXY", "no_proxy"):
        _cur = os.environ.get(_k, "")
        for _h in ("127.0.0.1", "localhost"):
            if _h not in _cur:
                os.environ[_k] = (_cur + "," if _cur else "") + _h
    _defuse_stdout()
    if _all_stop_requested():
        _log("logs/all.stop 在：当前是全关状态，不启动（拉起我的：%s）" % _parent_cmdline())
        _el = _exitlog()
        if _el:
            _el.record("supervisor", why="all_stop", detail="启动时 logs/all.stop 在")
        return 0
    if not _acquire_mutex():
        _log("已有监管器在跑，本实例退出（本 pid=%s，拉起我的：%s）" % (os.getpid(), _parent_cmdline()))
        _el = _exitlog()
        if _el:
            _el.record("supervisor", why="already_running", detail="互斥体已被占用")
        return 0
    _sched = _scheduler_alive()
    _eng = liveness.state("engine")
    if not _take_over(_sched, _eng):
        _log("主程序与引擎心跳都新鲜，说明已有实例在跑，监管器退出（拉起我的：%s）" % _parent_cmdline())
        _el = _exitlog()
        if _el:
            _el.record("supervisor", why="already_running", detail="心跳都新鲜")
        return 0
    if _sched:
        _log("主程序心跳还在但引擎心跳是 %s：接管（清僵尸锁后重启）" % _eng)
    # 2026-10-26：这里是"启动"的唯一入口，所以由它负责把上一次"全关"留下的自停哨兵
    # 清干净；否则开机自启会一上来就被 scheduler.stop / loop.stop 立刻叫停。
    for name in _STOP_SENTINELS:
        try:
            os.remove(os.path.join(LOGS, name))
            _log("清掉上次留下的 logs/%s" % name)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log("清 logs/%s 失败：%r" % (name, exc))
    _write_lock()
    stop_evt = threading.Event()
    _defuse_stdout()
    threading.Thread(target=_watch_all_stop, args=(stop_evt,), daemon=True,
                     name="supervisor-all-stop").start()
    threading.Thread(target=_heartbeat_loop, args=(stop_evt,), daemon=True,
                     name="supervisor-heartbeat").start()
    backoff = 2.0
    starts = 0
    last_reap = 0.0
    last_beat_check = 0.0
    try:
        while True:
            if _all_stop_requested():
                _log("收到 logs/all.stop：全关，监管器退出")
                _el = _exitlog()
                if _el:
                    _el.record("supervisor", why="all_stop", detail="收到 logs/all.stop")
                _press_sentinels("all.stop")
                return 0
            _clear_stale_lock()
            starts += 1
            _log("启动第 %d 次：run.py scheduler" % starts)
            try:
                proc = subprocess.Popen([PY, "run.py", "scheduler"], cwd=ROOT,
                                        creationflags=CREATE_NO_WINDOW)
            except KeyboardInterrupt:
                return 0
            while True:  # 等子进程，但每一步都认总闸（总闸不靠子进程配合）
                rc = proc.poll()
                if rc is not None:
                    break
                # 2026-10-27：顺手清孤儿引擎（父进程已死的 loop.run，它会占着
                # single-instance 锁让新引擎起不来，而监管器本来根本看不见它）
                if time.time() - last_reap >= 5.0:
                    last_reap = time.time()
                    try:
                        _reap_orphan_engines(proc.pid)
                    except Exception as exc:  # noqa: BLE001
                        _log("清孤儿引擎异常：%r" % (exc,))
                if time.time() - last_beat_check >= 5.0:
                    last_beat_check = time.time()
                    try:
                        _check_engine()
                    except Exception as exc:  # noqa: BLE001
                        _log("查引擎心跳异常：%r" % (exc,))
                time.sleep(1.0)
            _log("子进程退出 rc=%s" % rc)
            _el = _exitlog()
            if _el:
                _el.record("scheduler", rc=rc, pid=proc.pid,
                           detail="监管器看到子进程退出")
            if _all_stop_requested():
                _log("logs/all.stop 在：监管器退出")
                _el = _exitlog()
                if _el is not None:
                    _el.record("supervisor", why="all_stop", detail="循环里看到 logs/all.stop")
                _press_sentinels("all.stop")
                return 0
            if rc == 0:
                # rc=0 有两种含义：①用户要停 ②子进程发现"已有实例在跑"自己退了。
                # 后者不能当停止处理，否则启动器+监管器抢着起时会被误判成用户点停。
                if _scheduler_alive():
                    _log("已有一份实例在跑（心跳/端口还在），监管器退出")
                    return 0
                _log("子进程用了 rc=0 但端口没起来，15 秒后重试（可能是启动竞争或僵尸锁）")
                _clear_stale_lock()
                time.sleep(15.0)
                continue
            if rc == RESTART_CODE:
                backoff = 2.0
                time.sleep(1.0)
                continue
            sleep_s = min(MAX_BACKOFF_S, backoff)
            _log("异常退出，%.0f 秒后重试" % sleep_s)
            time.sleep(sleep_s)
            backoff = min(MAX_BACKOFF_S, backoff * 2)
    finally:
        stop_evt.set()
        _drop_lock()

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
