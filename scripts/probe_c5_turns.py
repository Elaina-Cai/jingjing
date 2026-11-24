
import os,sys,json,asyncio,difflib
R=os.environ["YROOT"]; sys.path.insert(0,R); os.chdir(R)
from loop import snapshot, brain as B, ports
class FakeLLM:
    async def chat_tools(self, **kw):
        return {"content":"", "tool_calls":[], "tokens_in":0, "tokens_out":0,
                "usage":{}, "model":"fake", "provider":"fake"}
CAP={}
def stub(mod):
    mod.ledger.append_receipt = lambda *a, **k: None
    mod.ledger.quiet_ms = lambda *a, **k: None
    mod.ledger.blocked_targets = lambda *a, **k: {}
    mod.ports.resolve_say_target = lambda *a, **k: ("", "")
    mod.selfstate.touch_said = lambda *a, **k: None
    mod._dump_payload = lambda tid, msgs: CAP.__setitem__("m", json.loads(json.dumps(msgs)))
    class TT:
        def __init__(self,*a,**k): pass
        def __getattr__(self,n): return lambda *a,**k: None
    mod.trace_mod.TurnTrace = TT
stub(B)
snap = json.load(open(r".test_tmp/snap_fixture.json",encoding="utf-8"))
lab = "Elaina"; db = snap.get("db")
txt = snapshot.render_text(snap, only=lab)
turns = snapshot.render_turns(snap, lab, limit=15)
blk = "[原文]（这一窗，按时间顺序，不带注释）\n" + turns
async def go(st):
    CAP.clear()
    await B.run_loop(db, txt, snap_turns=st, llm=FakeLLM(), dry_run=True, focus=lab,
                     platform="wechat", chat_id="wxid_seed01", others=[])
    return CAP["m"]
old = asyncio.run(go("")); new = asyncio.run(go(blk))
u0=[m for m in old if m["role"]=="user"][0]["content"]; u1=[m for m in new if m["role"]=="user"][0]["content"]
s0=[m for m in old if m["role"]=="system"][0]["content"]; s1=[m for m in new if m["role"]=="system"][0]["content"]
print("system 相同:", s0==s1, len(s0), "->", len(s1))
print("user 字数:", len(u0), "->", len(u1), "(+%d)"%(len(u1)-len(u0)))
print("=== 新旧 user 的差量（只该多出原文那一段）===")
for ln in difflib.unified_diff(u0.splitlines(), u1.splitlines(), lineterm="", n=1):
    print(ln[:160])
print("=== 新 user 末尾 20 行 ===")
for ln in u1.splitlines()[-20:]: print("  |",ln[:120])
open(r".test_tmp/c5_new_user.txt","w",encoding="utf-8").write(u1)
open(r".test_tmp/c5_old_user.txt","w",encoding="utf-8").write(u0)
