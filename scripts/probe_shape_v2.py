
import os,sys,json,asyncio,difflib,re
R=os.environ["YROOT"]; sys.path.insert(0,R); os.chdir(R)
from loop import snapshot, brain as B
class FakeLLM:
    async def chat_tools(self, **kw):
        return {"content":"","tool_calls":[],"tokens_in":0,"tokens_out":0,"usage":{},"model":"fake","provider":"fake"}
CAP={}
B.ledger.append_receipt=lambda *a,**k: None
B.ledger.quiet_ms=lambda *a,**k: None
B.ledger.blocked_targets=lambda *a,**k: {}
B.ports.resolve_say_target=lambda *a,**k: ("","")
B.selfstate.touch_said=lambda *a,**k: None
B._dump_payload=lambda tid,msgs: CAP.__setitem__("m", json.loads(json.dumps(msgs)))
class TT:
    def __init__(self,*a,**k): pass
    def prompt(self, system="", parts=None): CAP["parts"]=[(k,v) for k,v in (parts or [])]
    def __getattr__(self,n): return lambda *a,**k: None
B.trace_mod.TurnTrace=TT
snap=json.load(open(".test_tmp/snap_fixture.json",encoding="utf-8"))
db=snap.get("db"); label="Elaina"; chat="wxid_seed01"
txt=snapshot.render_text(snap, only=label, back=False)
turns=snapshot.render_turns(snap, label, limit=15)
blk="[原文]（这一窗，按时间顺序，不带注释）\n"+turns
async def go():
    CAP.clear()
    await B.run_loop(db, txt, snap_turns=blk, llm=FakeLLM(), dry_run=True, focus=label,
                     platform="wechat", chat_id=chat, others=[])
    return CAP["m"], CAP["parts"]
def side(ms):
    return ([m for m in ms if m["role"]=="system"][0]["content"], [m for m in ms if m["role"]=="user"][0]["content"])
import copy
os.environ.pop("PROMPT_SHAPE_V2",None); v1, p1 = asyncio.run(go())
os.environ["PROMPT_SHAPE_V2"]="1"; v2, p2 = asyncio.run(go())
s1,u1=side(v1); s2,u2=side(v2)
def show(tag,parts,sysc,usrc):
    print(f"=== {tag}: system {len(sysc)} / user {len(usrc)} ===")
    for k,t in parts: print(f"   {len(t):5d}  {k}")
show("V1", p1, s1, u1); show("V2", p2, s2, u2)
all2 = s2+"\n\n"+u2
print("\n=== 段落守恒（V1 每段在 V2 里出现几次）===")
bad=[]
for k,t in p1:
    tt=t.strip()
    if not tt: continue
    n=all2.count(tt)
    print(f"   {n}  {len(tt):5d}  {k}")
    if n!=1: bad.append((k,n))
print("   异常:", bad or "无")
print("\n=== V2 最后一段是不是原文:", u2.rstrip().endswith(turns.rstrip()))
print("=== V2 里有没有重复的整段 ===")
dup=[(k,t[:30]) for k,t in p2 if (s2+u2).count(t.strip())>1 and len(t.strip())>40]
print("   ", dup or "无")
