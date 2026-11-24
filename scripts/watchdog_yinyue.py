#!/usr/bin/env python
"""鲸鲸看门狗：**只读探测 + 必要时拉起**，把"她静默离线"变成看得见的事实。

为什么要有这一条
----------------
2026-10-02 18:08，服务被外部终止（计划任务 ``yinyue_scheduler`` 的
``LastTaskResult`` = 3221225786 = ``0xC000013A``，即 Ctrl+C / 控制台窗口被关掉
把进程树一起带走；没有 traceback，日志里一个字都没有），**直到 22:00 主人手工
发现才重启——整整 3 小时 45 分钟，她不在线，而没有任何人知道**。日志只能证明
"她曾经在跑"，证明不了"她现在还在"；所以这里不看日志，直接问活口。

口径（探测顺序 **API 优先**）
-----------------------------
1. ``GET {url}/api/status``：唯一能证明"是她本人在答"的口径；拿到 JSON 且带
   ``started_at`` 才算在线（顺带能看出"是不是刚起来的新进程"）；
2. 退而求其次 ``socket.create_connection((host, port))``：只有"端口在 Listen"
   这一条证据（可能是别的进程占着，所以排第二）；
3. 两次探测（``--interval``，默认 5 秒）**都失败**才判"离线"——单次失败可能只是
   GC 抖动 / 一次慢请求，误判会去动生产，代价比晚 5 秒高得多。

主人暂停：``logs/watchdog.stop`` 哨兵（**拉起之前的最后一道闸**）
--------------------------------------------------------------
项目既有的约定（``scripts/yinyue_launcher.py``：启动时**删**哨兵，停止时**建**哨兵
并写"已停止端口占用进程 …（并放置 logs/watchdog.stop 防 autostart 拉起）"）：
**哨兵存在 = 主人有意让服务停着**。所以判"离线"之后、动手之前先看它一眼——
哨兵在就记一条 ``action="halted"``、控制台说一句"主人已暂停（哨兵在）"，
**不拉起、不告警、exit 0**：暂停是主人的意图，不是故障；计划任务只按退出码判成败，
非零会把"主人就是要她停着"标成一次失败，纯属噪声。
哨兵**不优先于在线**：她还在答话时什么都不做，也不多写一行。

判"离线"之后
------------
``Start-ScheduledTask -TaskName yinyue_scheduler``（**只拉起一次**，不新建第二个
实例：`MultipleInstances IgnoreNew` + 拉起前再确认端口没在 Listen）。拉起后再探一次：

* 回来了 → 记一条 ``action="recovered"``；
* 仍失败 → 记一条 ``action="alert"``，note 里写"拉起失败"（**不静默**）。

告警落点：``logs/watchdog.jsonl``（追加，一行一条 JSON）+ 控制台。
**故意不发群消息**——看门狗不该拿主人的社交关系当报警器；
它只负责把事实写进文件并给出非零退出码，让 Task Scheduler 也记一笔
（唯一的例外是"主人暂停"那条路：见上，它是 exit 0）。

绝不做的事
----------
不 import ``core.*``、不跑迁移、不碰任何库——探测期间连生产库都不该被打开。
本脚本只用标准库。

用法
----
    .\\.venv\\Scripts\\python.exe scripts\\watchdog_yinyue.py              # 在线即 exit 0
    .\\.venv\\Scripts\\python.exe scripts\\watchdog_yinyue.py --json       # 打印判定结果
    # 假探测点（只验证判定与告警路径，不碰真服务）：
    .\\.venv\\Scripts\\python.exe scripts\\watchdog_yinyue.py \\
        --status-url http://127.0.0.1:1/api/status --port 1 \\
        --alert-file .tmp\\wd_probe.jsonl --interval 0.2

退出码：0 = 在线（没动过手）**或主人已暂停（哨兵在）**；1 = 离线过，但现在回来了；
2 = 自己救不回来。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:   # 直接 python scripts\watchdog_yinyue.py 时 sys.path[0] 是 scripts/
    sys.path.insert(0, str(ROOT))

SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 39871
TASK_NAME = "yinyue_scheduler"
LOG_FILE = "logs/yinyue.log"
ALERT_FILE = "logs/watchdog.jsonl"
LOCK_FILE = "logs/watchdog.lock"

#: 两次探测之间的间隔；"两次都失败"才判离线（见模块 docstring）。
PROBE_INTERVAL_SECONDS = 5.0
#: 单次探测的超时（API 与端口各算一次）。
PROBE_TIMEOUT_SECONDS = 3.0
#: 拉起之后给服务多少秒起来（调度器 import 要几秒，太短会误判"拉起失败"）。
STARTUP_GRACE_SECONDS = 20.0
#: ``logs/yinyue.log`` 超过这么久没动静 → 单独记一条 ``stale_log`` 当线索。
STALE_LOG_SECONDS = 900.0
#: 拉起命令的超时。
TASK_START_TIMEOUT_SECONDS = 20.0
#: 告警文件超过这么大就先截断（免得没人看时无限涨）。
ALERT_MAX_BYTES = 2 * 1024 * 1024
#: 上一次看门狗还没跑完就又来一次 → 直接让位（``logs/watchdog.lock`` 的陈旧阈值）。
LOCK_STALE_SECONDS = 600.0

#: 暂停哨兵的有效期：超过这么久就当"忘了删"（2026-11-04 E-55：一个上午留下的
#: ``logs/watchdog.stop`` 让她躺了五个半小时，而告警只写"主人已暂停"，读起来像是
#: 主人故意的 —— 一个没有时效的哨兵，等于一个没人会发现的静音键）。
PAUSE_MAX_HOURS = 2.0
#: 服务日志超过这么久没更新，就单独记一条 ``down_alert``（每小时最多一次）。
DOWN_ALERT_HOURS = 1.0
DOWN_ALERT_EVERY_HOURS = 1.0

#: 2026-11-05（E-75 之后）：她哑多久算异常。引擎可以在线、窗口也可以在，但她一个字都没发出去 ——
#: 昨夜就是这样躺了 18 小时没人知道。阈值由主人定（6 小时），改这里或改 .env 都行。
QUIET_ALERT_HOURS = 6.0

EXIT_ONLINE = 0
EXIT_RECOVERED = 1
EXIT_FAILED = 2
#: 主人已暂停（``logs/watchdog.stop`` 哨兵在）→ **正常收工**，所以是 0 不是 1/2：
#: 计划任务只按退出码判成败，非零会把"主人有意让她停着"记成一次失败。
EXIT_HALTED = 0


def pause_sentinel_path(root: Path | None = None) -> Path:
    """暂停哨兵路径：``<仓库根>/logs/watchdog.stop``。

    **口径照 ``scripts/yinyue_launcher.py``**（``self.root / "logs" / "watchdog.stop"``，
    见其 ``start()`` 与 ``stop()``）：不新造路径规则，两边指的是同一个文件。
    """
    return (Path(root) if root is not None else ROOT) / "logs" / "watchdog.stop"


def pause_sentinel_active(path: Path) -> bool:
    """哨兵在不在；查不到就当"不在"——一个读不到的文件不该让看门狗瘫掉。"""
    try:
        return Path(path).exists()
    except OSError:  # noqa: BLE001 - 探测路径本身出问题，按"没有哨兵"处理
        return False


def pause_sentinel_age_hours(path: Path, *, now: float | None = None) -> float | None:
    """哨兵放了多久（小时）；文件不在/读不到 → ``None``（"没有"不等于"刚刚"）。"""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return max(0.0, ((time.time() if now is None else float(now)) - st.st_mtime) / 3600.0)


def pause_sentinel_expired(path: Path, *, now: float | None = None,
                           max_hours: float = PAUSE_MAX_HOURS) -> tuple[bool, float | None]:
    """哨兵算不算"过期"（忘了删）。返回 ``(是否过期, 放了多久)``。

    纯判断，只 ``stat`` 一个文件：过期 ＝ 哨兵还在，但已经比 ``max_hours`` 更久。
    这样"主人有意停两小时"仍然是停，"停了五个半小时没人管"会被照常拉起并留告警。
    """
    age = pause_sentinel_age_hours(path, now=now)
    if age is None:
        return (False, None)
    return (age >= float(max_hours), age)


def down_alert_due(offline_hours: float, last_alert_hours: float | None, *,
                   after_hours: float = DOWN_ALERT_HOURS,
                   every_hours: float = DOWN_ALERT_EVERY_HOURS) -> bool:
    """要不要为"她已经停机这么久"单独喊一声。

    ``offline_hours``：这次停机已经多久；``last_alert_hours``：上一次为此喊话距今多久
    （``None`` ＝这个停机里还没喊过）。纯算术，不读文件、不看时钟。
    """
    if float(offline_hours) < float(after_hours):
        return False
    if last_alert_hours is None:
        return True
    return float(last_alert_hours) >= float(every_hours)


@dataclass
class Probe:
    """一次探测的证据（``ok`` 才是结论，``detail`` 只是给人看的）。"""

    ok: bool
    kind: str = "?"          # api | port | none
    detail: str = ""
    started_at: str = ""
    payload: dict = field(default_factory=dict)


def now_iso() -> str:
    """带本地时区偏移的 ISO8601——事后对时间线时不用猜是哪个时区。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _http_probe(url: str, timeout: float) -> Probe:
    """``GET {url}/api/status``：拿到 JSON 就算在线，取得到 ``started_at`` 更佳。"""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - 本机回环
        raw = resp.read(64 * 1024)
        code = int(getattr(resp, "status", 0) or getattr(resp, "code", 0) or 0)
    if code and code >= 400:
        # 服务在答，但这个口子坏了（认证/路由）——不能当"在线"。
        return Probe(False, "api", "HTTP %d" % code)
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return Probe(False, "api", "HTTP %s 但不是 JSON" % (code or "?"))
    if not isinstance(payload, dict) or "started_at" not in payload:
        # 答话的不是鲸鲸（代理/别的服务占着这个端口）。
        return Probe(False, "api", "JSON 里没有 started_at")
    started = str(payload.get("started_at") or "")
    return Probe(True, "api", "HTTP %d（started_at=%s）" % (code or 200, started or "?"),
                 started_at=started, payload=payload)


def _port_probe(host: str, port: int, timeout: float) -> Probe:
    """退而求其次：端口在 Listen（不问内容，所以只是弱证据）。"""
    with socket.create_connection((host, int(port)), timeout=timeout):
        return Probe(True, "port", "%s:%d 在 Listen" % (host, port))


def _probe(host: str, url: str, port: int, timeout: float) -> Probe:
    """探测一次：**API 优先**，失败再试端口。

    任何异常（超时 / 连接拒绝 / 名字解析）都当场降级成"这次失败"，
    **绝不让栈冲出去**——看门狗自己崩掉是最糟的结果：它一崩，
    就再没有任何东西知道她离线了。
    """
    try:
        return _http_probe(url, timeout)
    except Exception as exc:  # noqa: BLE001 - 探测失败就是失败，不抛
        api_detail = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    try:
        return _port_probe(host, port, timeout)
    except Exception as exc:  # noqa: BLE001 - 同上（ConnectionRefused/timeout 都在这）
        return Probe(False, "none",
                     "API 失败（%s）；端口失败（%s: %s）"
                     % (api_detail, type(exc).__name__, str(exc)[:120]))


def _sleep(seconds: float) -> None:
    """独立出来是为了测试能把它换成 no-op（用例不该真等 5 秒）。"""
    if seconds > 0:
        time.sleep(seconds)


class ProbeSequence:
    """探测序列：失败就等 ``interval`` 再探，**两次都失败**才判离线。

    ``probe`` 正常不该抛（默认 ``_probe`` 自己就把一切都收成失败）；万一它还是抛了
    （换了个坏替身 / 以后有人改坏了），这里按"这次失败"计数——**看门狗自己崩掉是
    最糟的结果**：它一崩，就再没有任何东西知道她离线了。
    """

    def __init__(self, *, attempts: int = 2, interval: float = PROBE_INTERVAL_SECONDS,
                 probe: Callable[..., Probe] | None = None, sleep: Callable[[float], None] | None = None):
        self.attempts = max(1, int(attempts))
        self.interval = float(interval)
        self._probe = probe if probe is not None else _probe
        self._sleep = sleep if sleep is not None else _sleep
        self.probes: list[Probe] = []

    def run(self, *, host: str, url: str, port: int, timeout: float) -> Probe:
        """返回**最后一个**探测结果；全部失败时它就是"离线"的证据。"""
        result = Probe(False, "none", "没探测")
        for index in range(self.attempts):
            if index:
                self._sleep(self.interval)
            try:
                result = self._probe(host=host, url=url, port=port, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - 探测件自己炸了也算这次失败
                result = Probe(False, "none", "探测异常：%s: %s"
                               % (type(exc).__name__, str(exc)[:120]))
            self.probes.append(result)
            print("[watchdog] 第 %d/%d 次探测：%s %s"
                  % (index + 1, self.attempts, "ok" if result.ok else "失败", result.detail))
            if result.ok:
                return result
        return result


#: 监管器心跳（scripts/supervisor.py 每 5 秒写一次）超过这个秒数没动 = 挂死。
#: 挂死的监管器占着单实例互斥体、对 all.stop 无反应，普通拉起会被弹开 ——
#: 永远救不回来（2026-11-01 深夜实锤过一次），必须先杀树。
SUPERVISOR_HEARTBEAT_STALE_SECONDS = 120.0


def supervisor_hung(*, path: Path | None = None, now: float | None = None) -> tuple[bool, str]:
    """监管器还活着吗？心跳文件 120 秒没动 = 挂死。

    心跳文件不存在或读不懂 = 老版本监管器（没有心跳）或本来就没跑 —— 都不算挂，
    交给正常拉起路径去处理。
    """
    hb = Path(path) if path is not None else ROOT / "logs" / "supervisor_heartbeat.json"
    try:
        raw = json.loads(hb.read_text(encoding="utf-8"))
        pid = int(raw.get("pid") or 0)
        ts = float(raw.get("ts") or 0.0)
    except Exception:  # noqa: BLE001 - 没有/读不懂都不算挂
        return False, "无心跳文件（老版监管器或未运行）"
    age = max(0.0, (now if now is not None else time.time()) - ts)
    if age <= SUPERVISOR_HEARTBEAT_STALE_SECONDS:
        return False, "心跳新鲜（%.0f 秒前，pid=%d）" % (age, pid)
    return True, "心跳 %.0f 秒没动（pid=%d）" % (age, pid)


def kill_supervisor_tree(pid: int) -> str:
    """杀挂死的监管器整棵树（互斥体随进程死亡释放）。"""
    try:
        done = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                              capture_output=True, encoding="utf-8", errors="replace",
                              timeout=15)
        detail = (done.stderr or done.stdout or "").strip().replace("\n", " ")[:100]
        return "taskkill exit=%d %s" % (done.returncode, detail)
    except Exception as exc:  # noqa: BLE001 - 杀失败也要把事实带回去
        return "taskkill 没跑起来：%s" % str(exc)[:120]


def run_task_start(task: str = TASK_NAME, *, timeout: float = TASK_START_TIMEOUT_SECONDS,
                   command: str | None = None) -> Probe:
    """拉起：``Start-ScheduledTask -TaskName <task>``。

    **只拉起一次、不新建第二个实例**：`Start-ScheduledTask` 只是让计划任务按它
    自己的动作启动（任务本身带 ``MultipleInstances=IgnoreNew``），这里绝不自己
    ``Popen`` 一个服务进程——那条路才会双开。命令不存在 / 拒绝访问一律算失败，
    由调用方写告警，不静默。

    ``command`` 只在**演练/自测**里用（见 ``--task-start-cmd``）：把这一格换成
    替身命令，这样"离线判定 → 拉起 → 复探"的整条路能真跑一遍，而生产服务一根
    手指都不碰。默认 None = 真去 Start-ScheduledTask。
    """
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-Command",
           command if command else "Start-ScheduledTask -TaskName '%s'" % task]
    try:
        # 显式 utf-8：子进程的 stderr 是中文 PowerShell 文本，按系统代码页解会糊成乱码。
        done = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 拉起失败也是一种结论，不能抛
        return Probe(False, "task", "拉起命令没跑起来：%s" % str(exc)[:160])
    if done.returncode != 0:
        err = (done.stderr or done.stdout or "").strip().replace("\n", " ")[:200]
        return Probe(False, "task", "拉起命令失败（exit=%d）：%s" % (done.returncode, err))
    return Probe(True, "task", "已 Start-ScheduledTask -TaskName %s" % task)


def append_alert(path: Path, payload: dict, *, max_bytes: int = ALERT_MAX_BYTES) -> None:
    """追加一条到 ``logs/watchdog.jsonl``；写不进去就在控制台喊（但不抛）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if path.exists() and path.stat().st_size > max_bytes:
                path.replace(path.with_suffix(path.suffix + ".1"))
                print("[watchdog] 告警文件超过 %d 字节，已轮转一份" % max_bytes)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:  # noqa: BLE001 - 告警落盘失败也必须留下痕迹
        print("[watchdog] !! 告警写不进 %s：%s" % (path, exc))


def _file_age_seconds(path: Path, now: float | None = None) -> float | None:
    """文件多久没动过了；文件不在或读不到 → None（不是 0，别把"没有"当"刚写过"）。"""
    try:
        return max(0.0, (now if now is not None else time.time()) - path.stat().st_mtime)
    except OSError:
        return None


def _recent_restart(path: Path, *, window: float, now: float) -> str:
    """窗口内是否已经拉起过（读自己写的 jsonl；防同一个停机被反复拉）。"""
    try:
        if path.stat().st_size > ALERT_MAX_BYTES:
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()[-50:]
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            _stderr("告警日志里有一行读不懂，跳过")
            continue
        if event.get("action") not in ("restarted", "recovered"):
            continue
        stamp = str(event.get("at") or "")
        try:
            elapsed = now - datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            continue
        if 0 <= elapsed < window:
            return "%s（%.0f 秒前）" % (stamp, elapsed)
    return ""


class _Lock:
    """单实例锁：两个看门狗同时判"离线"会各拉一次（第二次可能是双开的种子）。"""

    def __init__(self, path: Path, *, stale_after: float = LOCK_STALE_SECONDS):
        self.path = path
        self.stale_after = stale_after

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = _file_age_seconds(self.path)
            if age is None or age <= self.stale_after:
                return False
            with contextlib.suppress(OSError):
                self.path.unlink()
            try:
                handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                return False
        except OSError:
            return True  # 锁建不出来不该让看门狗瘫掉：宁可跑，也别不看
        with contextlib.suppress(OSError):
            os.write(handle, ("%d %s\n" % (os.getpid(), now_iso())).encode("utf-8"))
            os.close(handle)
        return True

    def release(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()


def _emit(alert_file: Path, event: dict, *, as_json: bool) -> None:
    """控制台 + JSONL 各留一份；``as_json`` 只影响控制台的多余输出。"""
    append_alert(alert_file, event)
    if as_json:
        print(json.dumps(event, ensure_ascii=False, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="鲸鲸看门狗：探测 127.0.0.1:39871，离线就 Start-ScheduledTask 拉起，并写告警。")
    parser.add_argument("--host", default=SERVICE_HOST, help="服务地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=SERVICE_PORT, help="端口（默认 39871）")
    parser.add_argument("--status-url", default=None,
                        help="状态接口完整 URL（默认由 --host/--port 拼；测试可指向假探测点）")
    parser.add_argument("--attempts", type=int, default=2,
                        help="判定离线前要连续失败几次（默认 2；1 = 关掉防抖，不建议）")
    parser.add_argument("--interval", type=float, default=PROBE_INTERVAL_SECONDS,
                        help="两次探测之间的间隔秒数（默认 5）")
    parser.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_SECONDS,
                        help="单次探测超时秒数（默认 3）")
    parser.add_argument("--task", default=TASK_NAME, help="拉起用的计划任务名（默认 yinyue_scheduler）")
    parser.add_argument("--grace", type=float, default=STARTUP_GRACE_SECONDS,
                        help="拉起后等多久再复探（默认 20 秒）")
    parser.add_argument("--cooldown", type=float, default=300.0,
                        help="告警文件里这个窗口内已经拉起过就不再拉（默认 300 秒）")
    parser.add_argument("--log-file", default=str(ROOT / LOG_FILE),
                        help="服务日志路径（只看 mtime，用来判断'连日志都不写了'）")
    parser.add_argument("--stale-log-seconds", type=float, default=STALE_LOG_SECONDS,
                        help="日志超过这么久没动静就记 stale_log（默认 900 秒）")
    parser.add_argument("--alert-file", default=str(ROOT / ALERT_FILE),
                        help="告警 jsonl（默认 logs/watchdog.jsonl）")
    parser.add_argument("--lock-file", default=str(ROOT / LOCK_FILE),
                        help="单实例锁文件（默认 logs/watchdog.lock）")
    parser.add_argument("--stop-file", default=str(pause_sentinel_path()),
                        help="主人暂停哨兵（默认 <仓库根>/logs/watchdog.stop，与 "
                             "yinyue_launcher.py 同一约定）：它在就只留痕、不拉起、不告警")
    parser.add_argument("--no-lock", action="store_true", help="不取单实例锁（只给测试用）")
    parser.add_argument("--json", action="store_true", help="把判定结果按 JSON 打到控制台")
    parser.add_argument("--dry-run", action="store_true",
                        help="探测照跑、告警照写，但**不真的拉起**（演示用）")
    parser.add_argument("--task-start-cmd", default=None,
                        help="演练钩子：用这条命令替掉 Start-ScheduledTask（自测/演练专用，"
                             "生产不要传）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    url = args.status_url or "http://%s:%d/api/status" % (args.host, args.port)
    alert_file = Path(args.alert_file)
    log_file = Path(args.log_file)
    stop_file = Path(args.stop_file)
    lock = _Lock(Path(args.lock_file))

    if not args.no_lock and not lock.acquire():
        print("[watchdog] 已有一个看门狗在跑（%s），本次让位" % args.lock_file)
        return EXIT_ONLINE
    try:
        return _run(args, url=url, alert_file=alert_file, log_file=log_file, stop_file=stop_file)
    finally:
        if not args.no_lock:
            lock.release()


def _run(args: argparse.Namespace, *, url: str, alert_file: Path, log_file: Path,
         stop_file: Path) -> int:
    started = time.monotonic()
    # 2026-11-05：先看她还在不在说话 —— 这一步跟端口通不通无关（昨夜端口通、她也哑着）。
    _check_quiet(alert_file, as_json=getattr(args, "json", False))
    # 2026-11-06：端口通不代表她还在 —— 心跳停了、或醒来发现中间有大空洞，都要说话。
    _check_engine(alert_file, as_json=getattr(args, "json", False), pause_file=stop_file)
    sequence = ProbeSequence(attempts=args.attempts, interval=args.interval)
    first = sequence.run(host=args.host, url=url, port=args.port, timeout=args.timeout)

    if first.ok:
        print("[watchdog] 在线（%s）→ 不动手；%s" % (first.detail, _log_line(log_file)))
        if args.json:
            print(json.dumps({"verdict": "online", "probe": first.detail,
                              "started_at": first.started_at}, ensure_ascii=False, sort_keys=True))
        return EXIT_ONLINE

    # ---- 到这里 = 连续多次都失败：判离线 ------------------------------------
    stale = _log_line(log_file)
    print("[watchdog] 判定离线：%d 次探测都失败（最后一次：%s）%s"
          % (len(sequence.probes), first.detail, stale))
    base = {
        "at": now_iso(),
        "before": first.started_at or "",
        "reason": first.detail,
        "note": "",
        "task": args.task,
        "probe_kind": first.kind,
        "attempts": len(sequence.probes),
        "url": url,
        "probe_detail": first.detail,
        "log_file": str(log_file),
        "log_age_seconds": _round(_file_age_seconds(log_file)),
    }
    if first.started_at:
        base["before"] = first.started_at

    # 2026-11-04 E-55：她停机很久时，光有 halted / alert 不够 —— 那两条都可能在
    # "主人已暂停"这条路上被跳过（哨兵在就直接 return，五个半小时一条告警都没有）。
    # 所以单独按"服务日志多久没写"判一次：超过 DOWN_ALERT_HOURS 就喊一声，每小时最多一次。
    _log_age = base.get("log_age_seconds")
    if isinstance(_log_age, (int, float)):
        _offline_h = float(_log_age) / 3600.0
        if down_alert_due(_offline_h, _last_down_alert_hours(alert_file)):
            _emit(alert_file, dict(base, action="down_alert", after="",
                                   offline_hours=_round(_offline_h),
                                   note="她已经 %.1f 小时没有动静（服务日志 %s 最后写入距今 %.0f 秒）。"
                                        "若这是主人有意停的，请删掉 %s，或把 PAUSE_MAX_HOURS 调大"
                                        % (_offline_h, log_file.name, float(_log_age), stop_file)),
                  as_json=args.json)
            _stderr("她已经 %.1f 小时没有动静（服务日志 %s 没更新）" % (_offline_h, log_file.name))

    # ---- 哨兵：主人有意让服务停着 → 这里不是故障，不拉起也不告警 ------------
    # 放在"在线"之后（在线优先：她还在答话就什么都不做），放在 dry-run / 冷却 /
    # 拉起之前（暂停比任何"要不要动手"的判断都优先，包括不该写 alert 的演练）。
    if pause_sentinel_active(stop_file):
        _expired, _age_h = pause_sentinel_expired(stop_file)
        if _expired:
            # 2026-11-04 E-55：哨兵没有时效时，一个忘了删的文件就能让引擎安静地躺一天，
            # 而告警只写"主人已暂停"。超过 PAUSE_MAX_HOURS 一律当忘了删：先留一条
            # 说得清的告警，然后照常往下走（冷却 / 挂死清理 / 拉起）。
            _emit(alert_file, dict(base, action="sentinel_expired", after="",
                                   stop_file=str(stop_file), pause_hours=_round(_age_h),
                                   note="暂停哨兵已放 %.1f 小时（上限 %.1f）：视为忘了删，"
                                        "继续尝试拉起" % (float(_age_h or 0.0), PAUSE_MAX_HOURS)),
                  as_json=args.json)
            _stderr("暂停哨兵已放 %.1f 小时，超过上限 %.1f 小时 → 视为忘了删，继续拉起"
                    % (float(_age_h or 0.0), PAUSE_MAX_HOURS))
        else:
            print("[watchdog] 主人已暂停（哨兵在）：%s → 不拉起、不告警" % stop_file)
            _emit(alert_file, dict(base, action="halted", after="",
                                   reason="%s；主人暂停哨兵在：%s" % (first.detail, stop_file),
                                   stop_file=str(stop_file), pause_hours=_round(_age_h),
                                   note="主人已暂停（哨兵在 %s）：不拉起、不告警；"
                                        "超过 %.1f 小时视为忘了删会自动恢复；"
                                        "要立刻恢复请删掉这个文件" % (stop_file, PAUSE_MAX_HOURS)),
                  as_json=args.json)
            return EXIT_HALTED

    if args.dry_run:
        _emit(alert_file, dict(base, action="alert", after="",
                               note="dry-run：判定离线但**没有真的拉起**；%s" % stale), as_json=args.json)
        print("[watchdog] dry-run：不执行 Start-ScheduledTask -TaskName %s" % args.task)
        return EXIT_FAILED

    # 2026-11-02：监管器挂死时（占互斥体、不响应 all.stop），普通拉起会被单实例
    # 锁弹开 —— 永远救不回来。心跳过期就先杀树再拉，并留一条告警。
    # 放在冷却检查**之前**：这不是一次"拉起尝试"，是清除路障；若放进冷却里，
    # 挂死期间每 5 分钟的探测都会被冷却跳过，她永远救不回来。
    hung, why = supervisor_hung()
    if hung:
        pid = 0
        try:
            raw = json.loads((ROOT / "logs" / "supervisor_heartbeat.json").read_text(encoding="utf-8"))
            pid = int(raw.get("pid") or 0)
        except Exception as exc:  # noqa: BLE001 - 读不到 pid 就没法杀，如实记录
            print("[watchdog] 监管器心跳读不出来（老版监管器或未运行）：%s" % repr(exc)[:80])
        kill_detail = kill_supervisor_tree(pid) if pid else "没拿到 pid，跳过（等人工处理）"
        _emit(alert_file, dict(base, action="killed_hung_supervisor", after="",
                               note="监管器挂死（%s）→ %s" % (why, kill_detail)),
              as_json=args.json)
        print("[watchdog] 监管器挂死已处置：%s；%s" % (why, kill_detail))
        if pid:
            time.sleep(1.5)

    recent = _recent_restart(alert_file, window=float(args.cooldown), now=time.time())
    if recent:
        note = "冷却中：%s 已经拉起过一次（%s），本次不再动计划任务" % (args.task, recent)
        _emit(alert_file, dict(base, action="cooldown", after="", note=note + "；" + stale),
              as_json=args.json)
        print("[watchdog] %s" % note)
        return EXIT_FAILED

    launch = run_task_start(args.task, command=args.task_start_cmd)
    print("[watchdog] 拉起：%s" % launch.detail)
    if not launch.ok:
        _emit(alert_file, dict(base, action="alert", after="",
                               note="拉起失败：%s；%s" % (launch.detail, stale)), as_json=args.json)
        _stderr("鲸鲸离线且拉不起来：%s" % launch.detail)
        return EXIT_FAILED

    print("[watchdog] 等 %.0f 秒再探一次…" % args.grace)
    _sleep(float(args.grace))
    after = _probe(host=args.host, url=url, port=args.port, timeout=args.timeout)
    print("[watchdog] 复探：%s %s" % ("ok" if after.ok else "失败", after.detail))
    elapsed = _round(time.monotonic() - started)

    if after.ok:
        _emit(alert_file, dict(base, action="recovered", after=after.started_at,
                               note="离线 %s 后拉起成功（%s），共 %s 秒"
                                    % (_offline_since(base, alert_file), launch.detail, elapsed)),
              as_json=args.json)
        print("[watchdog] 已恢复（started_at=%s）" % (after.started_at or "?"))
        return EXIT_RECOVERED

    _emit(alert_file, dict(base, action="alert", after="",
                           note="拉起失败：拉起后复探仍然失败（%s）；%s" % (after.detail, stale)),
          as_json=args.json)
    _stderr("鲸鲸离线，已拉起但复探仍失败：%s" % after.detail)
    return EXIT_FAILED


def _last_quiet_alert_hours(alert_file: Path, *, now: float | None = None) -> float | None:
    """上一条 ``quiet_alert`` 距今多少小时；没有/读不到 → ``None``（＝这条还没喊过）。"""
    try:
        lines = alert_file.read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return None
    base_now = time.time() if now is None else float(now)
    n_unreadable = 0
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            n_unreadable += 1
            continue
        if str(event.get("action") or "") != "quiet_alert":
            continue
        at = str(event.get("at") or "")
        try:
            return max(0.0, (base_now - datetime.fromisoformat(at).timestamp()) / 3600.0)
        except (TypeError, ValueError):
            n_unreadable += 1
            continue
    if n_unreadable:
        _stderr("告警日志 %s 有 %d 行读不懂：按「她哑了还没喊过」处理" % (alert_file, n_unreadable))
    return None


def quiet_alert_due(quiet_hours: float, last_alert_hours: float | None, *,
                    after_hours: float = QUIET_ALERT_HOURS,
                    every_hours: float = DOWN_ALERT_EVERY_HOURS) -> bool:
    """她哑了 ``quiet_hours`` 小时，该不该喊一声（同一个停机每小时最多一次）。"""
    try:
        quiet = float(quiet_hours)
    except (TypeError, ValueError):
        return False
    if quiet < float(after_hours):
        return False
    if last_alert_hours is None:
        return True
    try:
        # 2026-11-05：判的是「上次喊话到现在过了多久」，不是「她哑了多久减上次喊话多久」——
        # 后者在喊完之后 quiet 还在长，于是每 5 分钟喊一次（E-74 那种刷屏）。
        return float(last_alert_hours) >= float(every_hours)
    except (TypeError, ValueError):
        return True


#: 告警原文的固定片段。告警本身也是她账号发出去的 ``is_self=1``，会把静默计时清零；
#: 报给主人的数字要把这一步排除，否则永远显示「6.0 小时」。
QUIET_ALERT_MARK = "没有自己开口说过一句话"


def last_self_spoke_hours(root: Path | None = None, *, now: float | None = None,
                          ignore_alert: bool = False) -> float | None:
    """她最后一次自己说话（``messages.is_self = 1``）距今多少小时。

    ``ignore_alert=True`` 时不把 quiet_alert 自己那条算成「她开口了」。

    读不到任何账号库 / 一条自发消息都没有 → ``None``（＝这条判据今天不成立，
    **不许**把"查不到"当成"她哑了"）。
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    base_now = time.time() if now is None else float(now)
    newest: float | None = None
    for db_file in sorted(Path(base).glob("data/platforms/*/*/account.db")):
        try:
            # Path.as_uri() 会转义 # 和 ?；手拼 file: 在含这些字符的临时目录下会把路径截断。
            con = sqlite3.connect(Path(db_file).resolve().as_uri() + "?mode=ro", uri=True, timeout=3.0)
        except sqlite3.Error as exc:
            _stderr("账号库读不了（%s）：这次不算她哑了" % (db_file,))
            _ = exc
            continue
        try:
            sql = "select max(created_at) from messages where is_self = 1"
            if ignore_alert:
                # system 是「语音通话已结束」这类账号自动消息，不算她开口。
                sql += " and ifnull(msg_type_name,'') not in ('system')"
                sql += " and ifnull(content,'') not like ?"
            if ignore_alert:
                row = con.execute(sql, ("%" + QUIET_ALERT_MARK + "%",)).fetchone()
            else:
                row = con.execute(sql).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            con.close()
        raw_ts = str((row or [None])[0] or "")
        if not raw_ts:
            continue
        try:
            ts = datetime.strptime(raw_ts[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            _stderr("这条时间戳读不懂（%s），按没有算" % (raw_ts[:24],))
            continue
        if newest is None or ts > newest:
            newest = ts
    if newest is None:
        return None
    return max(0.0, (base_now - newest) / 3600.0)


def _owner_env(root: Path | None = None) -> int:
    """把 `.env` 里的两个主人 id 读进环境，返回读到的个数。

    2026-11-05 踩到的坑：计划任务 `\\yinyue_watchdog` 是用 `pythonw.exe` 直接跑本脚本的，
    **不经过 `run.py`**，所以 `.env` 从来没被加载 → `owner_ids()` 是空的 →
    每条 `quiet_alert` 在 jsonl 里都记着 `owner_copy: no_owner`，主人一次也没收到过。
    只挑这两个键注入（别把整个 .env 灌进短命进程的环境），已有值不覆盖。
    """
    base = Path(root) if root else ROOT
    p = base / ".env"
    if not p.exists():
        return 0
    n = 0
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            key, val = s.split("=", 1)
            key = key.strip()
            if key not in ("WECHAT_OWNER_WXIDS", "YINYUE_OWNER_WXIDS"):
                continue
            if key in os.environ:
                continue
            os.environ[key] = val.strip().strip('"').strip("'")
            n += 1
    except OSError as exc:  # 读不到就按「没有 .env」处理，但要说出来
        _stderr("读 .env 失败：%r" % (exc,))
    return n


def _notify_owner(quiet_hours: float, *, root: Path | None = None) -> str:
    """哑太久时也发一条给主人（2026-11-05 主人「要」）。

    走 ``loop.owner_copy`` 那条队列（``turn_id="owner_copy"``），不直连微信：
    这里是每 5 分钟就起一次的短命进程，拿不住键鼠，也不该拿。
    返回一句话说明发没发出去（写进 alert 那一行，事后能查）；
    **抄送失败绝不影响告警本身** —— 告警是底线，抄送是加分。
    """
    _owner_env(root)
    try:
        import sys as _sys
        if str(ROOT) not in _sys.path:
            _sys.path.insert(0, str(ROOT))
        from loop import owner_copy as _oc
        got = _oc.note("quiet_alert", detail="%.1f" % float(quiet_hours))
        return str((got or {}).get("why") or ("sent" if (got or {}).get("sent") else "no_owner"))
    except Exception as _exc:  # noqa: BLE001 - 抄送坏了不许影响告警
        _stderr("抄送主人失败：%r" % (_exc,))
        return "failed"


# ---- 引擎心跳：她「在不在」不靠端口、也不靠日志新旧（2026-11-06 §47）---------
#: 心跳停止更新多久算「她不会说话了」（心跳每 15 秒一次，900 秒 = 连丢 60 次）。
ENGINE_HEARTBEAT_STALE_SECONDS = 900.0
#: 醒来第一跳发现「上一跳到现在」隔了多久才算一段空白（睡眠/挂起）。
ENGINE_GAP_ALERT_HOURS = 1.0


def _beat_default() -> Path:
    """心跳文件在哪儿只问 ``liveness``：名字写在这一层就会被抄成第二份（守卫会拦）。"""
    from core.infra import liveness as _lv

    return Path(_lv.path("engine"))


def engine_beat(*, path: Path | None = None, now: float | None = None) -> dict:
    """读引擎心跳，算出「多久没动」与「最近一段空白多长」。

    读不到/读不懂一律 ``ok=False`` 并且**不算故障**：还没起过、或者主人按了全关，
    都会是这个样子。少喊一次不等于可以不记；但把「不在」当成「坏了」会天天误报。
    """
    p = Path(path) if path is not None else _beat_default()
    base_now = time.time() if now is None else float(now)
    got = {"ok": False, "age_s": None, "gap_hours": None, "gap_at": "", "pid": None,
           "at_text": "", "gap_prev_text": "", "note": ""}
    try:
        raw = p.read_bytes().decode("utf-8")
    except OSError:
        got["note"] = "没有心跳文件（还没起过 / 被删了）"
        return got
    try:
        data = json.loads(raw)
    except ValueError:
        got["note"] = "心跳文件读不懂"
        return got
    if not isinstance(data, dict) or not data.get("at_ms"):
        got["note"] = "心跳里没有 at_ms"
        return got
    at_ms = int(data.get("at_ms") or 0)
    got["ok"] = True
    got["pid"] = data.get("pid")
    got["at_text"] = str(data.get("at_text") or "")
    got["age_s"] = max(0.0, base_now - at_ms / 1000.0)
    gap_ms = int(data.get("gap_ms") or 0)
    if gap_ms:
        got["gap_hours"] = gap_ms / 3600000.0
        gap_at_ms = int(data.get("gap_at_ms") or 0)
        if gap_at_ms:
            got["gap_at"] = datetime.fromtimestamp(gap_at_ms / 1000.0).isoformat(timespec="seconds")
        got["gap_prev_text"] = str(data.get("gap_prev_text") or "")
    return got


def _last_action_hours(alert_file: Path, action: str, *, now: float | None = None) -> float | None:
    """上一条 ``action`` 距今多少小时；没有/读不到 → ``None``（＝这个还没喊过）。"""
    try:
        lines = Path(alert_file).read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return None
    base_now = time.time() if now is None else float(now)
    n_unreadable = 0
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            n_unreadable += 1
            continue
        if event.get("action") != action:
            continue
        try:
            at = datetime.fromisoformat(str(event.get("at") or "")).timestamp()
        except ValueError:
            return None
        return max(0.0, (base_now - at) / 3600.0)
    if n_unreadable:
        _stderr("告警日志 %s 有 %d 行读不懂：按「这个还没喊过」处理" % (alert_file, n_unreadable))
    return None


def _last_engine_gap_at(alert_file: Path) -> str:
    """上一条 ``engine_gap`` 报的是哪一段空白（按引擎给的 ``gap_at`` 去重，不按小时数）。"""
    try:
        lines = Path(alert_file).read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return ""
    n_unreadable = 0
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError as exc:
            n_unreadable += 1
            _ = exc
            continue
        if event.get("action") == "engine_gap":
            return str(event.get("gap_at") or "")
    if n_unreadable:
        _stderr("告警日志 %s 有 %d 行读不懂：按「这段空白还没报过」处理"
                % (alert_file, n_unreadable))
    return ""


def _check_engine(alert_file: Path, *, as_json: bool = False, now: float | None = None,
                  beat_path: Path | None = None, pause_file: Path | None = None) -> dict:
    """她「在不在」：机器睡过去、进程被挂起、心跳停了 —— 这三件都不是「她不肯开口」。

    2026-11-06：02:54 机器进了 Modern Standby，11.7 小时后才醒；``exits.jsonl`` 把它记成
    ``orphan_cleaned``（读起来像崩溃），唯一喊出来的 ``quiet_alert`` 又说成「她不肯开口」。
    所以这里单独一条：读引擎自己的心跳，空了多久就直说多久。
    """
    base_now = time.time() if now is None else float(now)
    got = engine_beat(path=beat_path, now=base_now)
    if not got.get("ok"):
        return got
    paused = bool(pause_file is not None and pause_sentinel_active(Path(pause_file)))
    gap_h = float(got.get("gap_hours") or 0.0)
    gap_at = str(got.get("gap_at") or "")
    if gap_h >= ENGINE_GAP_ALERT_HOURS and gap_at and gap_at != _last_engine_gap_at(alert_file):
        prev_text = str(got.get("gap_prev_text") or "（上一次心跳）")
        _emit(alert_file,
              {"at": now_iso(), "action": "engine_gap", "reason": "gap", "note": "",
               "gap_at": gap_at, "gap_hours": _round(gap_h), "gap_prev_text": prev_text,
               "engine_pid": got.get("pid"),
               "note2": "她有 %.1f 小时没有任何心跳（%s 之后那段空白）：这段时间她不在 —— "
                        "机器睡眠、或者进程被挂起。不是她不肯开口。"
                        % (float(gap_h), prev_text)},
              as_json=as_json)
        _stderr("她有 %.1f 小时没有任何心跳（%s 之后）" % (float(gap_h), prev_text))
    age_s = got.get("age_s")
    if age_s is not None and float(age_s) > ENGINE_HEARTBEAT_STALE_SECONDS and not paused:
        last_h = _last_action_hours(alert_file, "engine_stale", now=base_now)
        if last_h is None or float(last_h) >= DOWN_ALERT_EVERY_HOURS:
            _emit(alert_file,
                  {"at": now_iso(), "action": "engine_stale", "reason": "stale", "note": "",
                   "heartbeat_age_s": int(age_s), "at_text": str(got.get("at_text") or ""),
                   "engine_pid": got.get("pid"),
                   "note2": "她的心跳已经 %d 秒没有更新（最后一次 %s）：进程可能还在、端口可能也通，"
                            "但她已经不会说话了。" % (int(age_s), got.get("at_text") or "未知")},
                  as_json=as_json)
            _stderr("她的心跳已经 %d 秒没有更新" % int(age_s))
    return got


def _check_quiet(alert_file: Path, *, as_json: bool = False, root: Path | None = None,
                 now: float | None = None) -> float | None:
    """她哑太久就喊一声（引擎在线也照喊 —— 这正是昨夜没人发现的那种故障）。"""
    # 2026-11-09：人工全关期间不该喊「她不开口」—— 那是人按的停止，不是故障。
    # 不加这条时，all.stop 挂着的这几天看门狗每小时往 outbound_queue 写一条
    # quiet_alert，解闸后会连发给主人（11-06~09 实测堆了 54 条，已手工清理）。
    base = Path(root) if root is not None else ROOT
    if (base / "logs" / "all.stop").exists():
        return None
    quiet_h = last_self_spoke_hours(root, now=now)
    if quiet_h is None:
        return None
    if not quiet_alert_due(quiet_h, _last_quiet_alert_hours(alert_file, now=now)):
        return quiet_h
    # 判据仍按含告警的 quiet_h（保持「六小时起、至少隔一小时」的节奏），
    # 但报给主人的数字要真实：排除上一条告警自己，否则永远是 6.0。
    honest_h = last_self_spoke_hours(root, now=now, ignore_alert=True)
    shown_h = float(quiet_h) if honest_h is None else max(float(quiet_h), float(honest_h))
    _own = _notify_owner(shown_h)
    _emit(alert_file, {"at": now_iso(), "action": "quiet_alert", "reason": "", "note": "",
                       "quiet_hours": _round(shown_h),
                       "owner_copy": _own,
                       "note2": "她已经 %.1f 小时没有自己说过一句话了：引擎在跑、窗口也在，"
                                "但话没发出去。也可能这段时间机器睡眠 / 进程被挂起过 —— "
                                "看 logs/watchdog.jsonl 里有没有 engine_gap 行；再看 logs/desk.log 与 "
                                "outbound_queue 里最近的 failed / unverified 行" % float(shown_h)},
          as_json=as_json)
    _stderr("她已经 %.1f 小时没有自己说过一句话了（阈值 %.1f）"
            % (float(shown_h), QUIET_ALERT_HOURS))
    return quiet_h



def _last_down_alert_hours(alert_file: Path, *, now: float | None = None) -> float | None:
    """上一条 ``down_alert`` 距今多少小时；没有/读不到 → ``None``（＝这个停机还没喊过）。"""
    try:
        lines = alert_file.read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return None
    base_now = time.time() if now is None else float(now)
    n_unreadable = 0
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            n_unreadable += 1
            continue
        if event.get("action") != "down_alert":
            continue
        try:
            at = datetime.fromisoformat(str(event.get("at") or "")).timestamp()
        except ValueError:
            return None
        return max(0.0, (base_now - at) / 3600.0)
    if n_unreadable:
        # 读不懂的行不能白丢：告警日志自己坏了，"没喊过"就会被当成事实（E-56）。
        _stderr("告警日志 %s 有 %d 行读不懂：按「这个停机还没喊过」处理" % (alert_file, n_unreadable))
    return None


def _offline_since(base: dict, alert_file: Path) -> str:
    """这次停机大概从什么时候开始：上一次看到"在线"的影子（``before`` 或最后一条告警）。"""
    if base.get("before"):
        return str(base["before"])
    try:
        lines = alert_file.read_text(encoding="utf-8").splitlines()[-20:]
    except OSError:
        return "未知起点"
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("at"):
            return str(event["at"])
    return "未知起点"


def _log_line(log_file: Path) -> str:
    age = _file_age_seconds(log_file)
    if age is None:
        return "服务日志 %s 读不到" % log_file
    return "服务日志 %s 最后写入距今 %.0f 秒" % (log_file.name, age)


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def _stderr(message: str, stream=None) -> None:
    """拉起失败要能在"人看得见的地方"喊一声（计划任务的输出会被吞）。

    ``stream`` 默认在**调用时**取 ``sys.stderr``（不是 import 时抓一把）：
    这样测试能替掉它，也不会被重定向玩坏。
    """
    print("[watchdog] !! %s" % message, file=stream if stream is not None else sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
