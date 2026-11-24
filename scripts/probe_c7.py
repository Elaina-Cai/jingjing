
import os,sys,json,asyncio
R=os.environ["YROOT"]; sys.path.insert(0,R); os.chdir(R)
from loop import snapshot, brain as B, ledger
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
# 假台账：同一拍里，别的窗口已经发过两句
tick="2026-10-31 13:40:00"
B.ledger.read_receipts=lambda limit=200: [
  {"at":"2026-10-31 13:40:31","act":"say","result":"sent","target":"wxid_seed16","text":"在 我看着呢"},
  {"at":"2026-10-31 13:40:44","act":"say","result":"sent","target":"10000000002@chatroom","text":"这句是真长的测试"*6},
  {"at":"2026-10-31 13:40:50","act":"say","result":"sent","target":"wxid_seed01","text":"这一句是本窗自己说的 不该出现在去重段"},
  {"at":"2026-10-31 13:40:52","act":"blocked","result":"blocked","target":"wxid_x","text":"被挡的话不算"},
  {"at":"2026-10-31 13:39:00","act":"say","result":"sent","target":"old","text":"上一拍的话 不算"},
]
snap=json.load(open(".test_tmp/snap_fixture.json",encoding="utf-8"))
db=snap.get("db"); label="Elaina"; chat="wxid_seed01"
txt=snapshot.render_text(snap, only=label, back=False)
turns=snapshot.render_turns(snap, label, limit=15)
blk="[原文]（这一窗，按时间顺序，不带注释）\n"+turns
async def go(v2):
    CAP.clear()
    if v2: os.environ["PROMPT_SHAPE_V2"]="1"
    else: os.environ.pop("PROMPT_SHAPE_V2",None)
    await B.run_loop(db, txt, snap_turns=blk, llm=FakeLLM(), dry_run=True, focus=label,
                     platform="wechat", chat_id=chat, others=[], tick_since=tick)
    return CAP["m"]
for v2 in (False, True):
    ms=asyncio.run(go(v2))
    print("=== V2" if v2 else "=== V1", "===")
    for role in ("system","user"):
        c=[m["content"] for m in ms if m["role"]==role][0]
        i=c.find("这一拍")
        print(f"  {role} {len(c)} 字 | 「这一拍…」@{i}")
        if i>=0: print("   >>>", c[i:c.find('\n\n',i+1)][:400].replace("\n","\n   >>> "))
    # 打印 parts 名
    print("  parts:", [k for k,_ in CAP["parts"]])
