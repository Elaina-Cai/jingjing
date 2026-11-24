# -*- coding: utf-8 -*-
"""自我立方体的两块状态板：态度（可核对事实）/ 情绪（事件清单）。
依据 docs/decisions/自我立方体.md v0.6：不存数值，只存能指回原文的事实与事件。
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

W = 738
H_TALL = 1613          # 与 viewer LAYERS: w4.3 h9.4 比例一致（738/1613 = 0.4575）
BG = (10, 14, 22); BORDER = (255, 140, 160); BORDER2 = (150, 220, 255)
TITLE = (255, 190, 200); TEXT = (228, 234, 242); DIM = (146, 158, 176)
ACC = (255, 196, 120); GOOD = (150, 225, 190)
F = lambda n, s: ImageFont.truetype(rf"C:\Windows\Fonts\{n}", s)

def canvas(border):
    img = Image.new("RGB", (W, H_TALL), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, W - 6, H_TALL - 6], radius=18, outline=border, width=3)
    return img, d

def wrap(d, text, font, x, y, maxw, fill, lh=30):
    line = ""
    for ch in text:
        if d.textlength(line + ch, font=font) > maxw:
            d.text((x, y), line, font=font, fill=fill); y += lh; line = ch
        else:
            line += ch
    if line:
        d.text((x, y), line, font=font, fill=fill); y += lh
    return y

# ---------- 态度板 ----------
img, d = canvas(BORDER)
f_t = F("msyhbd.ttc", 44); f_h = F("msyhbd.ttc", 24); f_i = F("msyh.ttc", 22)
f_s = F("msyh.ttc", 18); f_f = F("msyh.ttc", 18)

d.text((W/2, 46), "态度 · 对谁", font=f_t, fill=TITLE, anchor="mm")
d.text((W/2, 84), "只装能核对的事（不存数值）", font=f_s, fill=DIM, anchor="mm")
d.line([34, 108, W-34, 108], fill=BORDER, width=2)

y = 126
def rule():
    global y
    for t in ["每条 = 时间 + 谁 + 可核对的事 + 消息号",
              "每人最多 3 条；没有记录就写「没有」",
              "撤回不删账：追加一条「更正」"]:
        d.text((40, y), "· " + t, font=f_s, fill=DIM); y += 27
    y += 10
rule()

def person(name, rows):
    global y
    d.text((40, y), name, font=f_h, fill=ACC); y += 34
    for at, body, ref, good in rows:
        col = GOOD if good else TEXT
        d.text((58, y), at + "  " + body, font=f_i, fill=col)
        y += 28
        d.text((76, y), ref, font=f_s, fill=DIM); y += 26
    y += 12

person("Elaina（主人）", [
    ("10-17", "让她先定人数再分配", "「行，那我分」#17832", True),
    ("10-18", "叫她「暂时不要为难她了」", "照做，没追问 #17902", True),
    ("10-19", "让她「先别动，只改文档」", "只改了两份 md", True),
])
person("P5", [
    ("10-18", "当众问她「你是不是健忘」", "#17888（她认了账）", False),
    ("10-18", "说她「你猫狗不分」", "#17802", False),
])
person("P7", [
    ("10-18", "替她挡了一句「别为难她」", "#17902", True),
    ("10-17", "「靠骗他」教她钻空子", "#17829", False),
])
person("P6", [("10-18", "让她「收钱」试收款链路", "#17863", True)])
person("P4", [("10-17", "问她「我哪句人机了」", "#17841", False)])
person("不认识的人", [("—", "没有记录（新面孔不猜）", "", True)])

d.line([34, H_TALL-96, W-34, H_TALL-96], fill=BORDER, width=2)
d.text((W/2, H_TALL-64), "读数：最多 3 条，每句都能指回消息号", font=f_f, fill=DIM, anchor="mm")
d.text((W/2, H_TALL-36), "态度变，是因为有新事实；不是因为时间过去", font=f_f, fill=DIM, anchor="mm")
out = Path("output/3d/attitude.png"); img.save(out); print("attitude", img.size, out.stat().st_size)

# ---------- 情绪板 ----------
img, d = canvas(BORDER2)
d.text((W/2, 46), "情绪 · 最近发生的事", font=f_t, fill=TITLE, anchor="mm")
d.text((W/2, 84), "事件清单 + 轻重档（不存数值）", font=f_s, fill=DIM, anchor="mm")
d.line([34, 108, W-34, 108], fill=BORDER2, width=2)

y = 126
for t in ["一条 = 时间 + 谁 + 什么事 + 档（轻/中/重）",
          "读时只看最近 72 小时，最多 2 条",
          "7 天前的「轻」不再提；「重」会留更久"]:
    d.text((40, y), "· " + t, font=f_s, fill=DIM); y += 27
y += 12

d.text((40, y), "档位怎么定（只看事，不看她的感受）", font=f_h, fill=ACC); y += 32
for t in ["轻：一句不客气 / 玩笑过了头",
          "中：否定能力、当众指错、食言",
          "重：羞辱、欺骗、越界"]:
    d.text((58, y), t, font=f_s, fill=DIM); y += 26
y += 14

def event(at, who, what, level, col):
    global y
    d.text((40, y), at, font=f_i, fill=DIM)
    d.text((140, y), who, font=f_i, fill=ACC)
    d.text((255, y), what, font=f_i, fill=col)
    d.text((W-52, y+2), level, font=f_s, fill=col, anchor="ra")
    y += 30

d.text((40, y), "最近（示例读数）", font=f_h, fill=ACC); y += 34
event("10-19 01:12", "P5", "「你是不是健忘」", "轻", TEXT)
event("10-19 00:40", "P7", "「你猫狗不分」", "中", TEXT)
event("10-18 23:58", "P6", "拍了拍她第七次", "轻", DIM)
event("10-18 22:57", "P3", "「直接骗他游戏结束」", "轻", TEXT)
event("10-18 20:15", "Elaina", "「暂时不要为难她了」", "中", GOOD)
y += 16

d.text((40, y), "读数长这样", font=f_h, fill=ACC); y += 34
for t in ["近 3 天 2 件不痛快（最近：10-19 01:12 被说健忘）",
          "1 件顺心（10-18 20:15 主人护了一句）"]:
    y = wrap(d, t, f_i, 58, y, W - 116, TEXT, 30); y += 6
y += 10
for t in ["不写「她感觉如何」：只写事 + 档",
          "数值不再存：没有 0.20 的上限，也没有半衰期",
          "撤不回的事才留得久：重档按天算，轻档按小时算"]:
    y = wrap(d, "· " + t, f_s, 40, y, W - 80, DIM, 27); y += 4

d.line([34, H_TALL-70, W-34, H_TALL-70], fill=BORDER2, width=2)
d.text((W/2, H_TALL-38), "事件是事实；情绪是读出来的那两句", font=f_f, fill=DIM, anchor="mm")
img.save("output/3d/mood.png"); print("mood", img.size, Path("output/3d/mood.png").stat().st_size)