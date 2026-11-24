
import os,sys,json,asyncio
R=os.environ["YROOT"]; sys.path.insert(0,R); os.chdir(R)
from loop import snapshot, brain as B, selfcard
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
def side(ms): return ([m for m in ms if m["role"]=="system"][0]["content"], [m for m in ms if m["role"]=="user"][0]["content"])
os.environ.pop("PROMPT_SHAPE_V2",None); v1,p1=asyncio.run(go())
os.environ["PROMPT_SHAPE_V2"]="1"; v2,p2=asyncio.run(go())
s1,u1=side(v1); s2,u2=side(v2)
card=selfcard.card_text(); sec=selfcard.card_sections()
print("卡 %d 字 | 节: %s"%(len(card), {k:len(v) for k,v in sec.items()}))
print("WALLS %d | SPEAK_GATE %d"%(len(B.WALLS), len(B.SPEAK_GATE)))
print("system: V1 %d -> V2 %d   (%+d)"%(len(s1),len(s2),len(s2)-len(s1)))
print("user  : V1 %d -> V2 %d   (%+d)"%(len(u1),len(u2),len(u2)-len(u1)))
all2=s2+"\n\n"+u2; all1=s1+"\n\n"+u1
print("\n=== 卡的每一行是否都在 V2 system 里 ===")
miss=[l for l in card.splitlines() if l.strip() and l.strip() not in s2]
print("   丢失行数:", len(miss), "| 例:", [m[:40] for m in miss[:5]])
print("=== V1 的每一行是否都在 V2 里（行级守恒）===")
ml=[l for l in all1.splitlines() if l.strip() and l.strip() not in all2]
print("   丢失行数:", len(ml), "| 例:", [m[:50] for m in ml[:6]])
print("\n=== V2 system 头 800 字 ===")
print(s2[:800])
print("\n=== V2 段来源顺序（parts）===")
for k,t in p2: print("  ", k, len(t))
