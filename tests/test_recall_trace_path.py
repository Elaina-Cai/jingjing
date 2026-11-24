"""召回 trace 的测试隔离：YINYUE_MEMORY_RECALL_TRACE_PATH 必须被 writer/reader 认。

归属：memory_recall_trace 日志（测试不得再写入真实 logs/，见交接文档 §7/§10）。
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core.terminal.memory_gateway as mg  # noqa: E402


class _Principal:
    chat_id = "iso@chatroom"
    platform = "wechat"
    bot_account_id = "bot_iso"


class _Hit:
    memory_id = "mem:iso1"
    score = 0.88


def test_recall_trace_honours_env_path() -> None:
    old_env = os.environ.get("YINYUE_MEMORY_RECALL_TRACE_PATH")
    old_path = mg._TRACE_PATH
    # 本沙箱 %TEMP% 禁写：用项目内目录（与 test_memory_sinking 同策略）。
    tmp_dir = Path("output") / f"_recall_iso_{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_dir / "recall_iso.jsonl"
    try:
        os.environ["YINYUE_MEMORY_RECALL_TRACE_PATH"] = str(target)
        mg._TRACE_PATH = None
        mg._trace_recall(_Principal(), query="隔离探针", hits=[_Hit()])
        assert target.exists(), "writer 没有写到 env 指定的路径"
        records = mg.read_recall_trace(
            chat_id="iso@chatroom", around=datetime.now(), window_seconds=120
        )
        assert records, "reader 没有从 env 指定的路径读回"
        assert records[-1]["query"] == "隔离探针", records[-1]
    finally:
        if old_env is None:
            os.environ.pop("YINYUE_MEMORY_RECALL_TRACE_PATH", None)
        else:
            os.environ["YINYUE_MEMORY_RECALL_TRACE_PATH"] = old_env
        mg._TRACE_PATH = old_path
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
