
import os,sys,json,asyncio,difflib
R=os.environ["YROOT"]; sys.path.insert(0,R); os.chdir(R)
from loop import snapshot, brain as B
class FakeLLM:
    async def chat_tools(self, **kw):
        return {"content":"", "tool_calls":[], "tokens_in":0, "tokens_out":0, "usage":{}}
CAP={}
def stub(m):
    m.ledger.append_receipt=lambda *a,**k: None; m.ledger.quiet_ms=lambda *a,**k: None
    m.ledger.blocked_targets=lambda *a,**k: {}
    m.ports.resolve_say_target=lambda *a,**k: ("","")
    m.selfstate.touch_said=lambda *a,**k: None
    m._dump_payload=lambda tid,msgs: CAP.__setitem__("m", json.loads(json.dumps(msgs)))
    class TT:
        def __init__(self,*a,**k): pass
        def __getattr__(self,n): return lambda *a,**k: None
    m.trace_mod.TurnTrace=TT
stub(B)
snap=json.load(open(r".test_tmp/snap_fixture.json",encoding="utf-8"))
lab="Elaina"; db=snap.get("db")
turns=snapshot.render_turns(snap,lab,limit=15)
blk="[原文]（这一窗，按时间顺序，不带注释）\n"+turns
async def go(txt, st):
    CAP.clear()
    await B.run_loop(db, txt, snap_turns=st, llm=FakeLLM(), dry_run=True, focus=lab,
                     platform="wechat", chat_id="wxid_seed01", others=[])
    return CAP["m"]
world_back = snapshot.render_text(snap, only=lab)              # 旧：带回看
world_slim = snapshot.render_text(snap, only=lab, back=False)  # 新：只留最新一条
print("世界段 字数 %d -> %d (-%d)"%(len(world_back),len(world_slim),len(world_back)-len(world_slim)))
old = asyncio.run(go(world_back, ""))          # 改造前的 prompt
new = asyncio.run(go(world_slim, blk))         # 现在的 prompt
u0=[m for m in old if m["role"]=="user"][0]["content"]; u1=[m for m in new if m["role"]=="user"][0]["content"]
print("user 总字数 %d -> %d (%+d)"%(len(u0),len(u1),len(u1)-len(u0)))
print("=== user 差量 ===")
for ln in difflib.unified_diff(u0.splitlines(), u1.splitlines(), lineterm="", n=0):
    if ln.startswith(('---','+++','@@')): continue
    print(ln[:130])
print("=== 新 user 全文 ===")
print(u1)
open(r".test_tmp/c5_slim_user.txt","w",encoding="utf-8").write(u1)
