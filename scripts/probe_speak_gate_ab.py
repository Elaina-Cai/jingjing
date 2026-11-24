"""A/B 探针：同一句 say 在「有跨窗硬闸」与「无闸」两种 brain.py 下的去向。
2026-10-30 撤闸时的验收证据。用法：.venv\\Scripts\\python.exe scripts/probe_speak_gate_ab.py
注意：BK 常量需指向一个「撤闸前」的 brain.py 备份（backups/gate-remove-*/loop__brain.py）。"""

import os, sys, json, types, asyncio
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
BK = os.path.join(ROOT, "backups", "gate-remove-20261031-002343", "loop__brain.py")

import loop.brain as NEW
from loop import ledger, ports, selfstate, trace as trace_mod

CAP = []
def fake_receipt(**kw):
    CAP.append(dict(kw)); return dict(kw)
ledger.append_receipt = fake_receipt
ledger.quiet_ms = lambda cid: None
ledger.blocked_targets = lambda *a, **k: {}
ports.resolve_say_target = lambda t: ("wxid_seed01", "")
class R:
    ok=True; text="已发出（replay）"; error=""; data={"ack":"replay"}
async def say_many(bubbles, dry_run=True, chat_id="", turn_id=""):
    return R()
ports.say_many = say_many
selfstate.touch_said = lambda *a, **k: None
class StubTrace:
    def __init__(self,*a,**k): pass
    def __getattr__(self, name):
        def _noop(*a, **k): return None
        return _noop
trace_mod.TurnTrace = StubTrace

SAY = {"content":"", "reasoning":"",
       "tool_calls":[{"name":"say","arguments":{"text":"没哎 今天挺安静的","target":"Elaina","why":"回一句"}}]}
END = {"content":"", "reasoning":"", "tool_calls":[]}

class FakeLLM:
    async def chat_tools(self, **kw):
        self.n = getattr(self,'n',0)+1
        return SAY if self.n == 1 else END

def run(mod, tag):
    CAP.clear()
    mod.trace_mod.TurnTrace = StubTrace
    mod.ledger = ledger; mod.ports = ports; mod.selfstate = selfstate
    try:
        rec = asyncio.run(mod.run_loop("world.db", "[别处也在动] 还有 7 个窗口有人在说话",
                                       llm=FakeLLM(), dry_run=True, others=["群A","群B"],
                                       focus="Elaina", platform="wechat", chat_id="wxid_seed01"))
        print(tag, "OK receipts=", [(r.get("act"), r.get("result")) for r in rec], "cap=", [(c.get("act"),c.get("result")) for c in CAP])
    except Exception as ex:
        import traceback; print(tag, "EXC", type(ex).__name__, ex)
        traceback.print_exc()

# 旧版：从备份源码加载成 loop.brain_old
src = open(BK, encoding="utf-8").read()
OLD = types.ModuleType("loop.brain_old"); OLD.__package__ = "loop"; OLD.__file__ = BK
sys.modules["loop.brain_old"] = OLD
exec(compile(src, BK, "exec"), OLD.__dict__)
run(OLD, "OLD(gate)")
run(NEW, "NEW(no-gate)")
