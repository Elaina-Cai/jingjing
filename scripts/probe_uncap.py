
import os,sys
R=os.environ["YROOT"]; sys.path.insert(0,R); os.chdir(R)
from loop import ledger
print("今天承诺段（真实台账）：")
t=ledger.render_promises()
print(t if t else "  （今天没有未销账的承诺 → 整段不出现）")
print("今天未销账条数:", len(ledger.open_promises()))
r=ledger.recent_say_targets(window_min=1440)
print("最近 24h 开过口的会话数:", len(r), [ (x["target"][:22], x["says"], x["last_text"][:40]) for x in r[:5] ])
