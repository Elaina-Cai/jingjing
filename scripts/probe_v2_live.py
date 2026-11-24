
import os,sys,json
R=os.environ["YROOT"]; sys.path.insert(0,R); os.chdir(R)
from loop import selfcard
dd=json.load(open("data/prompt/1a8a2eed8936/request.json",encoding="utf-8"))
ms=dd["rounds"][0]["messages"]
s=[m["content"] for m in ms if m["role"]=="system"][0]
sec=selfcard.card_sections()
for k,v in sec.items():
    lines=[l for l in v.splitlines() if l.strip()]
    miss=[l for l in lines if l.strip() not in s]
    print(f"{k}: {len(v)} 字, {len(lines)} 行, 丢 {len(miss)} 行", [m[:40] for m in miss[:3]])
part=sec.get("怎么说","")
print("\n别说的前 200 字:", part[:200].replace("\n","/"))
print("\nsystem 3400-3700:\n", s[3400:3700])
