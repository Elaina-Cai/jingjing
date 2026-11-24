from pathlib import Path
import subprocess
import sys


from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

# [诊断] faulthandler：定时 dump 所有线程栈（排查调度器卡死；诊断后可移除）
import faulthandler as _fh_mod
import threading as _th
import time as _time

_LOG_DIR = Path(__file__).resolve().parent / "logs"
_FH_PATH = _LOG_DIR / "faulthandler.log"


def _open_fh():
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return open(_FH_PATH, "a", buffering=1)


_fh_file = _open_fh()
_fh_mod.enable(file=_fh_file)


def _rotate_fh_if_needed() -> None:
    """faulthandler.log 只增长不切分：45s 一次的 dump 会让它无限涨。

    这里我们自己持有句柄，所以能干净地 close → rotate → reopen，不需要
    copy+truncate。策略来自 ``core.infra.log_rotate``（唯一来源，env 可覆盖/关闭）。
    """
    global _fh_file
    try:
        from core.infra.log_rotate import policy_for, rotate_if_needed

        policy = policy_for(_FH_PATH.name)
        if _FH_PATH.exists() and _FH_PATH.stat().st_size >= policy.max_bytes:
            _fh_mod.disable()
            _fh_file.close()
            rotate_if_needed(_FH_PATH)
            _fh_file = _open_fh()
            _fh_mod.enable(file=_fh_file)
    except Exception:  # noqa: BLE001 - 诊断设施不许拖垮主流程
        pass


def _dump_threads() -> None:
    while True:
        _time.sleep(45)
        _rotate_fh_if_needed()
        _fh_mod.dump_traceback(file=_fh_file)


_th.Thread(target=_dump_threads, daemon=True, name="faulthandler-dump").start()


def main():
    args = sys.argv[1:]
    if not args or args[0] == "scheduler":
        from scheduler.main import main as scheduler_main
        scheduler_main()
    elif args[0] in ("sync", "digest"):
        # 2026-10-26：sync/digest 是一次性服务入口（引擎的 kick_sync 也用它补世界）。
        # 它们必须自己声明可写生产库，否则 db.write_guard 会把整条同步拒成
        # "ProductionWriteRefused：data\yinyue_meta.db 这是生产库"，
        # 世界就永远停在旧的那一屏（当天 13:39 之后一条消息都没进来，就是这个原因）。
        from core.bootstrap import init_singletons
        init_singletons(allow_production=True)
        import asyncio
        if args[0] == "sync":
            from scheduler.main import run_sync_pipeline
            asyncio.run(run_sync_pipeline())
        else:
            from scheduler.main import run_daily_digest
            asyncio.run(run_daily_digest())
    else:
        print("Usage: python run.py [scheduler|sync|digest]")
        print("  scheduler  - Start the main scheduler (default)")
        print("  sync       - Run sync once")
        print("  digest     - Generate daily digest")
        print("  (微信密钥提取/首次启动请使用 python start.py)")


if __name__ == "__main__":
    # 2026-11-05（主人批的 ②）：调度器**自己**为什么退，也要留一行。
    # 之前只有监管器视角的「它退了，码是 X」—— 总闸 / Ctrl+C / 崩了 在那里长得一样。
    from core.infra.exitlog import wrap_main

    wrap_main(main, role="scheduler",
              detail="run.py %s" % (" ".join(sys.argv[1:]) or "scheduler"),
              stop_file=_LOG_DIR / "all.stop")
