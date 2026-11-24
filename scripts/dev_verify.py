"""开发/验证脚本的统一入口。

2026-10-28：之前我在外部脚本里跑视界渲染，日志一直刷

    [error] production_write_blocked  target=data\\yinyue_meta.db
    [warning] image_describe_failed   拒绝写入：… 这是生产库 …

原因是外部进程没有声明"可写生产库"，写口按设计拒绝。结论：验证脚本要显式声明。

用法（脚本第一行）：

    from scripts.dev_verify import bootstrap
    bootstrap()          # 默认 declared_by="debug"

它会做两件事：
  1. 把项目根加进 sys.path、chdir 到项目根；
  2. 读 .env 到 os.environ（只补缺，不覆盖已有的）；
  3. 调 db.write_guard.declare_writes_allowed(declared_by="debug")。

⚠️ 只在**你自己手动跑的验证/排障脚本**里用。任何被调度器/引擎/面板加载的代码
都不要调它——那条链的声明在 scheduler/main.py 的入口，边界就是靠这个分开的。
"""

from __future__ import annotations

import os
import sys


def _shutdown_pools() -> None:
    """退出前把已知的线程池等干净。

    2026-10-28：图片描述是提交到线程池异步做的，临时脚本渲染完就退，解释器关闭时
    线程池被掐 —— 日志里会出现

        image_describe_failed  error='cannot schedule new futures after interpreter shutdown'

    描述因此写不进缓存（对常驻的引擎没影响，只影响"我想看视界里图片描述对不对"）。
    这里在 atexit 里把模块级 ThreadPoolExecutor 逐个 shutdown(wait=True)。
    """
    import concurrent.futures as _cf

    mods = ("loop.vision", "core.conversation.media_ref", "core.preprocess.vision_runtime")
    for name in mods:
        try:
            mod = __import__(name, fromlist=["_"])
        except Exception:
            continue
        for attr, val in list(vars(mod).items()):
            if isinstance(val, _cf.ThreadPoolExecutor):
                try:
                    val.shutdown(wait=True)
                except Exception:
                    pass


def bootstrap(declared_by: str = "debug", *, env: str = ".env", wait_async: bool = True) -> str:
    """加载 .env + 声明可写生产库。返回项目根路径。

    wait_async=True（默认）时，注册 atexit 钩子在退出前等异步池做完 —— 否则图片描述
    这类后台任务会被解释器关闭掐断（见 :func:`_shutdown_pools`）。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        os.chdir(root)
    except OSError:
        pass
    path = os.path.join(root, env)
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    try:
        from db import write_guard

        write_guard.declare_writes_allowed(declared_by=declared_by)
    except Exception as exc:  # 声明失败就让调用方知道，而不是悄悄只读
        raise RuntimeError("声明可写生产库失败：%r" % (exc,))
    if wait_async:
        # 验证脚本生命周期短，后台线程在解释器关闭阶段还会调视觉描述，
        # 那时线程池已关、submit 会抛 "cannot schedule new futures…"。
        # 这里默认打开同步模式（media.py 支持后就不再碰线程池）。
        os.environ.setdefault("YINYUE_VISION_SYNC", "1")
        import atexit

        try:
            atexit.register(_shutdown_pools)
        except RuntimeError:
            # 解释器已经在收尾（脚本被 atexit 阶段的东西反向 import 时会发生），
            # 此时再注册会抛 "can't register atexit after shutdown"。不影响结果。
            pass
    return root
