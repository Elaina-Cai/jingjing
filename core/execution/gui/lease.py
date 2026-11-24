"""设备租约：一段时间内「手归谁」的独占凭证。

为什么需要它
------------
微信这扇窗口只认「当前前台 + 当前焦点」，两个任务同时动手会互相打断（一个刚切好会话，
另一个把鼠标挪走）。操作系统不替我们排队，所以排队这一层必须由我们自己做。

设计约束（见 docs/decisions/设备与并发.md §4 / §12）
------------------------------------------------
1. **只管物理动作那几秒**：持锁上限 2 秒量级；落盘回读（15–30 秒）必须放到锁外。
2. **租约不是安全边界**：它只防「无意绕过」，不防恶意代码——同机器上的任意进程仍可无视它。
3. **持有者要能被读出来**：拿不到锁时，调用方要知道「是谁在占」，才能决定等还是换路。
4. **崩溃要能自愈**：持锁进程被 kill 时 Windows 会释放文件锁；再加上 TTL / hard_ttl 双保险，
   不会出现「锁永远回不来」。
5. **hard_ttl 不可续期**：心跳只能把 ttl 往后推，不能把 created_at 往后推——否则一个卡死却
   还在发心跳的进程可以永占。``is_expired()`` 就是读这两个数判的。
6. **「谁活着」以文件锁为准，不以状态文件为准**：拿到排他锁就说明没有活进程持有它（Windows 在
   进程退出时自动释放），此时哪怕状态里写着别人、心跳还很新，也只是对方刚死没来得及改写——
   接管并记一条 warning。ttl/hard_ttl 用来判「这份状态还算不算新鲜」（给排障、给证据时效），
   不用来抢锁（别人的字节锁我们本来也解不开）。

实现
----
两道文件：``<名>.lock`` 只用来加锁（不写内容，避免截断破坏字节锁），``<名>.json`` 存持有者状态
（owner / pid / created_at / heartbeat_at / token）。取锁是「非阻塞锁 + 轮询到 wait_s」。

资源名（白名单，写错立刻报错而不是静默并行）
    ``input``            鼠标键盘独占（OS 输入，如 SendInput）
    ``screen``           截屏/OCR（与 input 分开：看不需要占手）
    ``evidence``         证据落盘（截图编号不能与别人穿插）
    ``page:<target>``    某个浏览器目标页（CDP 是页面内合成事件，**不需要** input 租约）
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from safelog import warn

try:  # pragma: no cover - 仅 Windows 有
    import msvcrt
except ImportError:  # pragma: no cover
    warn("core/execution/gui/lease.py:47 ImportError", err=repr(__import__("sys").exc_info()[1]))
    msvcrt = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
LOCK_DIR = ROOT / "data" / ".locks"

RESOURCE_INPUT = "input"
RESOURCE_SCREEN = "screen"
RESOURCE_EVIDENCE = "evidence"
PAGE_PREFIX = "page:"
RESOURCES = (RESOURCE_INPUT, RESOURCE_SCREEN, RESOURCE_EVIDENCE)

DEFAULT_TTL_S = 60.0
DEFAULT_HARD_TTL_S = 120.0
DEFAULT_WAIT_S = 8.0
DEFAULT_HEARTBEAT_S = 5.0
DEFAULT_POLL_S = 0.05


class UiBusy(RuntimeError):
    """等不到租约。``holder`` 是当前持有者（拿不到名字时为空串）。"""

    def __init__(self, resource: str, holder: str = "", waited_s: float = 0.0) -> None:
        self.resource = resource
        self.holder = holder
        self.waited_s = waited_s
        super().__init__(
            f"{resource} 被 {holder or '未知持有者'} 占着，等了 {waited_s:.1f}s 仍没拿到"
        )


class CapabilityMissing(RuntimeError):
    """环境不具备动手条件（锁屏、输入桌面不可写、权限不足…）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def check_resource(resource: str) -> str:
    """资源名白名单。拼错要立刻炸，不能静默并行。"""
    name = (resource or "").strip()
    if not name:
        raise ValueError("资源名不能为空")
    if name in RESOURCES:
        return name
    if name.startswith(PAGE_PREFIX) and len(name) > len(PAGE_PREFIX):
        return name
    raise ValueError(
        f"未知资源名 {resource!r}；只接受 {RESOURCES} 或 '{PAGE_PREFIX}<标签>'"
    )


def _safe_name(resource: str) -> str:
    out = resource
    for ch in (":", "/", chr(92), "*", "?", "<", ">", "|", '"', " "):
        out = out.replace(ch, "_")
    return out or "unnamed"


def _read_state(path: Path) -> dict[str, Any]:
    """读状态文件；读不到就当「没有状态」。

    ``OSError`` 要重试几次再放弃（2026-10-22 全量里的一次假红就是它）：Windows 上
    ``os.replace`` 与另一个进程/线程的 ``read_text`` 撞在一起时，读侧可能拿到
    ``PermissionError [WinError 32]``——文件本身是好的，只是这一刻被替换占了。
    以前这里把这类瞬时错误一律吞成 ``{}``，于是「谁占着」读成空串，
    ``test_device_lease`` 的「holder 是持有者」偶发变红。
    """
    for attempt in range(3):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            warn("core/execution/gui/lease.py:121 FileNotFoundError", err=repr(__import__("sys").exc_info()[1]))
            return {}
        except OSError:
            warn("core/execution/gui/lease.py:123 OSError", err=repr(__import__("sys").exc_info()[1]))
            if attempt == 2:
                return {}
            time.sleep(0.005)
            continue
        except Exception:
            warn("core/execution/gui/lease.py:128 Exception", err=repr(__import__("sys").exc_info()[1]))
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _write_state(path: Path, data: dict[str, Any]) -> None:
    """临时文件 + ``os.replace`` 原子写。

    临时名带随机后缀：同一份状态有两个写者（持锁线程的心跳 + 主线程的 ``release``），
    固定的 ``<名>.json.tmp`` 会让两个写者互相截断对方的临时文件，最后可能把**半份**
    JSON 装进状态文件——读侧只能当「没有状态」。
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            warn("core/execution/gui/lease.py:148 OSError", err=repr(__import__("sys").exc_info()[1]))
        raise


def read_state(resource: str, *, lock_dir: Path | str | None = None) -> dict[str, Any]:
    """只读当前状态（监控/排障用，不加锁）。"""
    base = Path(lock_dir) if lock_dir is not None else LOCK_DIR
    return _read_state(base / (_safe_name(check_resource(resource)) + ".json"))


def lock_is_free(resource: str, *, lock_dir: Any = None) -> bool:
    """这个资源的区间锁此刻是不是**没人持有**（探一下就放，不写状态）。

    为什么需要它：进程一死 OS 立刻释放区间锁，但 `data/.locks/*.json` 里的
    状态文件会留着。只看状态文件会把"锁空闲 + 状态过期"误判成"被占着"，
    于是一次非正常退出就让自检永远红。真话是：**锁空闲 = 可以开工**。
    """
    try:
        check_resource(resource)
    except Exception:  # noqa: BLE001 - 名字不合法就当"不空闲"
        warn("core/execution/gui/lease.py:168 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False
    base = Path(lock_dir) if lock_dir is not None else LOCK_DIR
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        warn("core/execution/gui/lease.py:173 Exception", err=repr(__import__("sys").exc_info()[1]))
        return False
    lock_path = base / (_safe_name(resource) + ".lock")
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        warn("core/execution/gui/lease.py:178 OSError", err=repr(__import__("sys").exc_info()[1]))
        return False
    try:
        if msvcrt is None:
            return True
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            warn("core/execution/gui/lease.py:186 OSError", err=repr(__import__("sys").exc_info()[1]))
            return False
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            warn("core/execution/gui/lease.py:191 OSError", err=repr(__import__("sys").exc_info()[1]))
        return True
    finally:
        os.close(fd)


def describe_holder(
    resource: str, *, lock_dir: Path | str | None = None, now: float | None = None
) -> dict[str, Any]:
    """谁占着、占了多久、心跳还新不新（排障与「是不是卡死」判定用，不加锁）。

    返回的字典一定含 ``holder`` / ``pid`` / ``heartbeat_age_s`` / ``age_s``：
    状态文件缺失时前两个是空串/0，后两个是 ``-1.0``（**不知道**，不是**刚心跳**）。
    调这个函数永远不会抛——排障信息拿不到不该改变失败方式。
    """
    try:
        state = read_state(resource, lock_dir=lock_dir)
    except Exception:  # noqa: BLE001 - 排障路径永不让调用方难做
        warn("core/execution/gui/lease.py:209 Exception", err=repr(__import__("sys").exc_info()[1]))
        state = {}
    stamp = time.time() if now is None else float(now)
    heartbeat = float(state.get("heartbeat_at") or 0.0)
    created = float(state.get("created_at") or 0.0)
    return {
        **state,
        "holder": str(state.get("owner") or ""),
        "pid": int(state.get("pid") or 0),
        "heartbeat_age_s": round(max(0.0, stamp - heartbeat), 3) if heartbeat else -1.0,
        "age_s": round(max(0.0, stamp - created), 3) if created else -1.0,
    }


class Lease:
    """一份租约。构造不动手，``acquire()`` 才动手（也可当上下文管理器）。"""

    def __init__(
        self,
        resource: str,
        owner: str,
        *,
        ttl: float = DEFAULT_TTL_S,
        hard_ttl: float = DEFAULT_HARD_TTL_S,
        wait_s: float = DEFAULT_WAIT_S,
        lock_dir: Path | str | None = None,
        clock=time.time,
        sleep=time.sleep,
        poll: float = DEFAULT_POLL_S,
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
        auto_heartbeat: bool = True,
    ) -> None:
        self.resource = check_resource(resource)
        self.owner = str(owner or "").strip() or "unknown"
        self.ttl = float(ttl)
        self.hard_ttl = float(hard_ttl)
        self.wait_s = float(wait_s)
        base = Path(lock_dir) if lock_dir is not None else LOCK_DIR
        base.mkdir(parents=True, exist_ok=True)
        self.lock_dir = base
        name = _safe_name(self.resource)
        self.lock_path = base / (name + ".lock")
        self.state_path = base / (name + ".json")
        self._clock = clock
        self._sleep = sleep
        self._poll = max(0.005, float(poll))
        self._heartbeat_s = max(0.05, float(heartbeat_s))
        self._auto_heartbeat = bool(auto_heartbeat)
        self._fd: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = {}
        self.token = ""

    # ---- 取锁 ---------------------------------------------------------
    def _try_lock(self) -> bool:
        if msvcrt is None:  # pragma: no cover
            raise CapabilityMissing("msvcrt_unavailable")
        try:
            fd = open(self.lock_path, "a+b")
        except OSError as exc:  # pragma: no cover - 磁盘不可写
            raise CapabilityMissing(f"lock_file_unwritable:{exc}") from exc
        try:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            warn("core/execution/gui/lease.py:274 OSError", err=repr(__import__("sys").exc_info()[1]))
            fd.close()
            return False
        self._fd = fd
        return True

    def _unlock(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover
            warn("core/execution/gui/lease.py:287 OSError", err=repr(__import__("sys").exc_info()[1]))
        finally:
            try:
                fd.close()
            except OSError:  # pragma: no cover
                warn("core/execution/gui/lease.py:292 OSError", err=repr(__import__("sys").exc_info()[1]))

    def _stale(self, state: dict[str, Any], now: float) -> bool:
        if not state:
            return True
        if state.get("released_at"):
            # 上次干净归还过：状态留着是给审计看的，不该再算「有人持有」
            return True
        try:
            created = float(state.get("created_at") or 0.0)
            beat = float(state.get("heartbeat_at") or 0.0)
        except (TypeError, ValueError):
            warn("core/execution/gui/lease.py:304 (TypeError, ValueError)", err=repr(__import__("sys").exc_info()[1]))
            return True
        if now - created >= self.hard_ttl:
            return True
        if now - beat >= self.ttl:
            return True
        return False

    def _claim(self) -> None:
        now = self._clock()
        self.token = uuid.uuid4().hex[:16]
        self.state = {
            "resource": self.resource,
            "owner": self.owner,
            "pid": os.getpid(),
            "token": self.token,
            "created_at": now,
            "heartbeat_at": now,
            "ttl": self.ttl,
            "hard_ttl": self.hard_ttl,
        }
        _write_state(self.state_path, self.state)
        if self._auto_heartbeat:
            self._thread = threading.Thread(
                target=self._beat_loop, name=f"lease-{self.resource}", daemon=True
            )
            self._thread.start()

    def acquire(self) -> "Lease":
        deadline = self._clock() + max(0.0, self.wait_s)
        holder = ""
        while True:
            if self._try_lock():
                # 拿到排他文件锁 = 没有任何活着的进程持有它（Windows 在进程退出时自动释放）。
                # 所以「状态里写着别人、心跳还很新」只可能是对方刚死/刚放而没来得及改写状态：
                # 这种矛盾以锁为准——接管，但留一条告警，别让它在日志里消失。
                state = _read_state(self.state_path)
                prev = str(state.get("owner") or "")
                if prev and prev != self.owner and not self._stale(state, self._clock()):
                    log.warning(
                        "lease state looked fresh but the lock was free; taking over "
                        "resource=%s prev_owner=%s prev_pid=%s",
                        self.resource, prev, state.get("pid"),
                    )
                self._claim()
                log.info(
                    "lease acquired resource=%s owner=%s token=%s",
                    self.resource, self.owner, self.token,
                )
                return self
            holder = str(_read_state(self.state_path).get("owner") or "")
            if self._clock() >= deadline:
                raise UiBusy(self.resource, holder, self.wait_s)
            self._sleep(self._poll)

    # ---- 续期 / 查询 ---------------------------------------------------
    def heartbeat(self) -> None:
        if self._fd is None:
            raise RuntimeError("尚未持有租约，不能续期")
        now = self._clock()
        if now - float(self.state.get("created_at") or now) >= self.hard_ttl:
            # hard_ttl 已到：不再续期（别人可以接管），也不报错——调用方自己决定是否放弃
            log.warning("lease hard_ttl reached resource=%s owner=%s", self.resource, self.owner)
            return
        self.state["heartbeat_at"] = now
        _write_state(self.state_path, self.state)

    def is_expired(self, *, now: float | None = None) -> bool:
        moment = self._clock() if now is None else float(now)
        state = self.state or _read_state(self.state_path)
        return self._stale(state, moment)

    def holder(self) -> str:
        return str((self.state or _read_state(self.state_path)).get("owner") or "")

    @property
    def held(self) -> bool:
        return self._fd is not None

    def _beat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_s):
            try:
                self.heartbeat()
            except Exception as exc:  # pragma: no cover - 心跳失败不该杀线程
                log.warning("lease heartbeat failed resource=%s err=%s", self.resource, exc)

    # ---- 归还 ---------------------------------------------------------
    def release(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        if self._fd is None:
            return
        try:
            state = dict(self.state)
            state["released_at"] = self._clock()
            state["released_by"] = self.owner
            _write_state(self.state_path, state)
        except Exception:  # pragma: no cover
            warn("core/execution/gui/lease.py:403 Exception", err=repr(__import__("sys").exc_info()[1]))
        self._unlock()
        log.info("lease released resource=%s owner=%s", self.resource, self.owner)

    def __enter__(self) -> "Lease":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def device_lease(
    resource: str,
    owner: str,
    *,
    ttl: float = DEFAULT_TTL_S,
    hard_ttl: float = DEFAULT_HARD_TTL_S,
    wait_s: float = DEFAULT_WAIT_S,
    lock_dir: Path | str | None = None,
) -> Lease:
    """一步到位：拿租约并返回（拿不到抛 :class:`UiBusy`）。"""
    return Lease(
        resource, owner, ttl=ttl, hard_ttl=hard_ttl, wait_s=wait_s, lock_dir=lock_dir
    ).acquire()
