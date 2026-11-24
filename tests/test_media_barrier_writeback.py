"""图像屏障的"描述要写回这一行"锁（2026-11-01）。

背景：`media_barrier.ask_once` 以前叫 `ensure_image_description(path)` 不传 engine，
内部回落到 `get_repo()`（loop 进程没有账号上下文 → 共享库），于是视觉算出来的描述
只进内存缓存、`message_media.description` 永远是 NULL；屏障每 15 分钟白等 20 秒还把
整拍跳过。写库被拒时又只 `log.debug`，所以谁都看不见。

这组锁只钉四件事，多了就脆：
1. `ensure_image_description` / `remember_image_description` 必须把 engine 传下去；
2. 落库失败必须 warn（不许再静默）；
3. `ask_once` 必须按 db_path 拿"这一行所在的库"的可写 engine；
4. `loop/run.py` 这个服务入口必须声明写入（否则守卫一定拒）。
"""
from __future__ import annotations

import ast
import sys as _sys
from pathlib import Path as _Path

_ROOT = str(_Path(__file__).resolve().parents[1])
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_remember_threads_engine_into_row_write():
    from core.conversation import media

    sig = inspect.signature(media.remember_image_description)
    assert "engine" in sig.parameters, "remember_image_description 必须能收 engine"
    src = _src("core/conversation/media.py")
    assert "_store_description_on_media_row(str(path), description, engine=engine)" in src
    inner = _src("core/conversation/media.py")
    assert "remember_description(engine if engine is not None else get_repo().engine" in inner


def test_persist_failure_is_warn_not_debug():
    src = _src("core/conversation/media.py")
    assert 'log.warning("media_description_persist_failed"' in src
    assert 'log.debug("media_description_persist_failed"' not in src


def test_ask_once_takes_db_path_and_passes_engine():
    from loop import media_barrier as B

    sig = inspect.signature(B.ask_once)
    assert "db_path" in sig.parameters
    src = _src("loop/media_barrier.py")
    assert "def _engine_for(db_path: str)" in src
    assert "ensure_image_description(path, engine=eng" in src
    assert "_engine_for(db_path) if db_path else None" in src


def test_wait_ready_threads_db_path_into_ask():
    src = _src("loop/media_barrier.py")
    assert "_ask_raw(row, per, db_path=db_path)" in src
    assert "pool.map(lambda r: _ask(r, per), batch)" in src


def test_loop_entry_declares_production_writes():
    src = _src("loop/run.py")
    tree = ast.parse(src)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "declare_writes_allowed"
    ]
    assert calls, "loop/run.py 没有调用 declare_writes_allowed"
    kws = {kw.arg for kw in calls[0].keywords}
    assert "declared_by" in kws
    assert 'declared_by="loop.run"' in src
def main() -> bool:
    """2026-11-02 补：这个文件以前只有 def test_、没有运行入口（一直空转，一条都没跑过）。"""
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
