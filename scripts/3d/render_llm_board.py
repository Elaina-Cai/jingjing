# -*- coding: utf-8 -*-
"""判/投/记 板：输入 · 产出 · 纪律 三段"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

W = H = 1200
BG = (11, 15, 24); BORDER = (120, 200, 255)
TITLE = (168, 226, 255); SUB = (150, 195, 220)
HDR = (255, 196, 120); KEY = (214, 232, 246); VAL = (168, 182, 198)
FOOT = (150, 162, 178)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
d.rounded_rectangle([8, 8, W-8, H-8], radius=26, outline=BORDER, width=4)
F = lambda n, s: ImageFont.truetype(rf"C:\Windows\Fonts\{n}", s)
f_t = F("msyhbd.ttc", 78); f_s = F("msyh.ttc", 30)
f_h = F("msyhbd.ttc", 38); f_k = F("msyh.ttc", 33); f_v = F("msyh.ttc", 30)
f_f = F("msyh.ttc", 26)

d.text((W/2, 78), "判 / 投 / 记", font=f_t, fill=TITLE, anchor="mm")
d.text((W/2, 148), "唯一有模型的地方 · 读三面 → 出判决", font=f_s, fill=SUB, anchor="mm")
d.line([54, 190, W-54, 190], fill=BORDER, width=3)

y = 214
def head(t):
    global y
    d.text((54, y), t, font=f_h, fill=HDR); y += 50
def row(k, v):
    global y
    d.text((70, y), k, font=f_k, fill=KEY)
    d.text((210, y + 2), v, font=f_v, fill=VAL)
    y += 44

head("【输入 · 只读，不产生判断】")
row("看", "世界面：这一屏原文 + 谁在回谁")
row("查", "自我面：情绪 / 态度 / 人设约束（只读）")
row("翻", "记忆面：按需翻，≤2 条带出处；翻不到就说不记得")
row("用", "工具：取料（搜网/读附件/看图）+ 操作（ui_control）")
y += 10

head("【产出 · 三必填 + 三条件】")
row("必填", "decision（respond/ask/silent/abstain）· deliver · basis")
row("条件①", "reading：只有开口时才要；沉默不写")
row("条件②", "open_loop：她答应的 / 别人欠她的（可空）")
row("条件③", "memory：0~3 条，只记会被再次指代的")
d.text((210, y-4), "每项都要 basis；kind 见出口注册表，未注册不许编", font=f_v, fill=VAL); y += 44
y += 10

head("【纪律】")
d.text((70, y), "· 指不回原文就 abstain；silent / abstain ⇒ deliver 必须空", font=f_v, fill=VAL); y += 40
d.text((70, y), "· 沉默/弃权只写判决行（decision；basis 可空），不写 reading", font=f_v, fill=VAL); y += 40
d.text((70, y), "· 说与做都是投递；同轮最多 1 条 say + 1 条动作，且指向同一件事", font=f_v, fill=VAL); y += 40
d.text((70, y), "· 不写原文、不改旧行、不打分类标签", font=f_v, fill=VAL); y += 40

d.line([54, H-64, W-54, H-64], fill=BORDER, width=2)
d.text((W/2, H-36), "出口只放行不判 · 未注册能力一律回答「做不到」", font=f_f, fill=FOOT, anchor="mm")

out = Path("output/3d/t_llm.png"); img.save(out)
print("wrote", out, out.stat().st_size, "bytes", img.size)