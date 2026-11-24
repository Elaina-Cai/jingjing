# -*- coding: utf-8 -*-
"""「工具包」内部清单板：分三组（读三面 / 对外动手 / 出口）"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

W, H = 738, 660
BG = (10, 14, 22); BORDER = (120, 200, 255)
TITLE = (150, 220, 255); TEXT = (228, 234, 242)
DIM = (146, 158, 176); ACC = (255, 196, 120)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
d.rounded_rectangle([6, 6, W - 6, H - 6], radius=18, outline=BORDER, width=3)
F = lambda n, s: ImageFont.truetype(rf"C:\Windows\Fonts\{n}", s)
f_t = F("msyhbd.ttc", 46); f_h = F("msyhbd.ttc", 24)
f_i = F("msyh.ttc", 23); f_d = F("msyh.ttc", 19); f_f = F("msyh.ttc", 19)

d.text((W/2, 46), "工具包 · LLM 的手脚", font=f_t, fill=TITLE, anchor="mm")
d.line([36, 78, W-36, 78], fill=BORDER, width=2)

y = 96
def group(title):
    global y
    d.text((40, y), title, font=f_h, fill=ACC); y += 34
def item(name, desc=""):
    global y
    d.text((58, y), name, font=f_i, fill=TEXT)
    if desc:
        d.text((W-52, y+2), desc, font=f_d, fill=DIM, anchor="ra")
    y += 30

group("① 读三面（专用通道 · 图上是直连管子）")
item("read_screen()", "这一屏")
item("get_state()", "我的状态")
item("search_memory(q, k≤2)", "按需翻记忆")
group("② 对外动手（走 run_tool）")
item("read_attachment(msg_id)", "读附件正文")
item("web_search(q) / fetch(url)", "搜网/抓页")
item("analyze_image(path)", "看图")
group("③ 操作电脑 / 软件（ui_control · 默认 shadow）")
item("ui_control(action, args)", "前台可见 · 标题匹配 · 截图∆≥100px")
group("④ 出口")
item("say(text, @)", "经出口注册表（未启用）")

d.line([36, H-64, W-36, H-64], fill=BORDER, width=2)
d.text((W/2, H-34), "run_tool(name, args) · 每回合 ≤2 次 · 用完要说明为什么", font=f_f, fill=DIM, anchor="mm")
out = Path("output/3d/t_toolbox.png"); img.save(out)
print("wrote", out, out.stat().st_size, "bytes")