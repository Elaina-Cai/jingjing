import os

# 测试里绝不自动拉起 WSL 推理服务（一次 60–80 秒，还会轮询 240 秒）
os.environ.setdefault('YINYUE_CV3_AUTOSTART', 'off')

import pytest


#: 每个测试都把「被拦下来的话」的旁证钉到临时目录。
#: 理由是这份文件是**生产旁证**：2026-11-05 它两次被测试写脏
#: （`logs/blocked.jsonl` 里出现 `c1`、`第一句 第二句` 这种只有测试才有的行）。
#: 真要读那份文件的用例，自己再 monkeypatch 覆盖这个属性即可。
@pytest.fixture(autouse=True)
def _blocked_log_isolated(tmp_path, monkeypatch):
    try:
        from loop import ports
    except ImportError:
        return None
    log = tmp_path / "blocked.jsonl"
    monkeypatch.setattr(ports, "BLOCKED_LOG", str(log), raising=False)
    return log


#: 静默告警同样钉到临时目录。`logs/silent_warnings.log` 是**生产旁证**，
#: verify_evidence_chain 的「没有静默熄火」就是数它；测试往里写 chat-0/same
#: 这类用例会话，会把那条判据弄成假红（2026-11-22 实测两条就是这么来的）。
@pytest.fixture(autouse=True)
def _silent_warnings_isolated(tmp_path, monkeypatch):
    try:
        import safelog
    except ImportError:
        return None
    log = tmp_path / "silent_warnings.log"
    monkeypatch.setattr(safelog, "LOG", str(log), raising=False)
    return log


#: 流水账本也要隔离：`data/loop/ledger-*.jsonl` 是「她最近开口」的判据来源，
#: 测试写进去的 say sent 会让她以为自己说过。run_all_tests 走 env 改道；
#: 直接 pytest 的没有那层，这里兜底。
@pytest.fixture(autouse=True)
def _ledger_dir_isolated(tmp_path, monkeypatch):
    try:
        from core.infra import actions
    except ImportError:
        return None
    ledger_dir = tmp_path / "ledger"
    monkeypatch.setattr(actions, "DIR", str(ledger_dir), raising=False)
    return ledger_dir


#: 她自己的状态表（data/loop/self_state.json）也是生产数据：心境、对某人的看法、
#: 最近说过的话。测试里写进去，下一拍她会带着测试语料说话；2026-11-24 加这层兜底。
@pytest.fixture(autouse=True)
def _self_state_isolated(tmp_path, monkeypatch):
    try:
        from loop import selfstate
    except ImportError:
        return None
    path = tmp_path / "self_state.json"
    monkeypatch.setattr(selfstate, "STATE", path, raising=False)
    return path
