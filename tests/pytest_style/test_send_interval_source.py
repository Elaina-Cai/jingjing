"""发车间隔只有一个来源（2026-10-28，PIT-111）。

两处各判一次间隔，会同时长出两种事故：
- 「刚发完又发」（一边撤了闸，另一边还在放行）
- 「一直被自己拦住」（负缓存自我续期，历史事故 2026-10-24 16:48）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from loop import ledger  # noqa: E402


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_quiet_ms_none_for_empty_target():
    assert ledger.quiet_ms("") is None
    assert ledger.quiet_ms("  ") is None


def test_quiet_ms_none_when_never_said():
    assert ledger.quiet_ms("__绝对不存在的人__") is None


def test_only_ledger_computes_interval():
    brain = _src(os.path.join("loop", "brain.py"))
    assert "ledger.quiet_ms(" in brain
    assert "time.time() - _last" not in brain


def test_runner_does_not_judge_per_target_interval():
    run = _src(os.path.join("loop", "run.py"))
    assert "last_say_at" not in run
    assert "ledger.quiet_ms(" not in run


def _src_sig(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8-sig") as f:
        return f.read().replace("\r\n", "\n")


def test_ledger_module_is_a_thin_shell():
    """流水只有一本，行格式只在一处：loop/ledger.py 不许再自己拼路径/自己追行。"""
    src = _src_sig(os.path.join("loop", "ledger.py"))
    assert "from core.infra import actions" in src
    assert "_actions.append_receipt" in src or "actions.append_receipt" in src
    assert '"a", encoding' not in src and "'a', encoding" not in src
    assert '"ledger-%s.jsonl"' not in src


def test_ledger_filename_is_constructed_in_one_place():
    """「ledger-<日期>.jsonl」这个名字只许 core/infra/actions.py 拼出来。

    只认格式化构造（ledger-%s / ledger_{}）；读的时候用 glob("ledger-*.jsonl") 是允许的。
    """
    hits = set()
    for sub in ("core", "loop", "db", "console", "scheduler", "agents", "scripts"):
        for dirpath, dirnames, files in os.walk(os.path.join(ROOT, sub)):
            dirnames[:] = [d for d in dirnames if not d.startswith("__Users__")]
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                fp = os.path.join(dirpath, fn)
                body = open(fp, encoding="utf-8-sig", errors="replace").read()
                if "ledger-%s" in body or "ledger_%s" in body or "ledger-{" in body:
                    hits.add(os.path.relpath(fp, ROOT).replace("\\", "/"))
    assert hits == {"core/infra/actions.py"}, hits


def test_proactive_service_asks_the_ledger():
    """冷却是「多久没动作」，只能问流水；冷却段里不许再读 DB 的 last_send_at。"""
    src = _src_sig(os.path.join("core", "terminal", "proactive_service.py"))
    assert "_actions.quiet_ms(" in src
    start = src.index('cooldown = int(getattr(target, "cooldown_minutes"')
    end = src.index('return ProactiveDecision(False, "cooldown", target)', start)
    block = src[start:end]
    assert "last_send_at(" not in block, block


def test_speech_path_writes_an_action_receipt():
    src = _src_sig(os.path.join("core", "speech", "outbound.py"))
    assert "_actions.append_receipt(" in src
    assert 'act="say"' in src


def test_both_layers_see_the_same_ledger(monkeypatch):
    from core.infra import actions
    from loop import ledger, ports

    assert ledger.DIR == actions.DIR
    assert ledger.path is actions.path
    seen = []
    monkeypatch.setattr(
        actions, "quiet_ms",
        lambda target, *, scope=(): seen.append((target, scope)) or 123,
    )
    scope = ports.ReadScope("wechat", "acct", "same")
    token = ports._READ_SCOPE.set(scope)
    try:
        assert ledger.quiet_ms("same") == 123
    finally:
        ports._READ_SCOPE.reset(token)
    assert seen == [("same", ("wechat", "acct", "same"))]
