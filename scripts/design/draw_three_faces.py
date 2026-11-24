from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

S = 2
W, H = 1060, 1120
img = Image.new("RGB", (W * S, H * S), (255, 255, 255))
d = ImageDraw.Draw(img)
F = "C:/Windows/Fonts/msyh.ttc"
FB = "C:/Windows/Fonts/msyhbd.ttc"
def f(sz, bold=False): return ImageFont.truetype(FB if bold else F, sz * S)
f_title, f_body, f_small, f_legend = f(19, True), f(16), f(14), f(15)

def txt(cx, cy, s, font, fill=(30, 38, 52)):
    bb = d.textbbox((0, 0), s, font=font)
    d.text((cx * S - (bb[2] - bb[0]) / 2, cy * S - (bb[3] - bb[1]) / 2 - bb[1]), s, font=font, fill=fill)

def box(x, y, w, h, title=None, lines=(), fill=(247, 250, 253), outline=(90, 120, 160), dash=False, tcolor=(20, 40, 70)):
    xy = [x * S, y * S, (x + w) * S, (y + h) * S]
    d.rounded_rectangle(xy, radius=10 * S // 2, fill=fill, outline=outline, width=2 * S)
    if title:
        if d.textlength(title, font=f_title) > (w - 18) * S: print("WARN title too wide:", title, d.textlength(title, font=f_title) / S, ">", w - 18)
        txt(x + w / 2, y + 20, title, f_title, tcolor)
    for i, ln in enumerate(lines):
        if d.textlength(ln, font=f_body) > (w - 12) * S: print("WARN line too wide:", ln, d.textlength(ln, font=f_body) / S, ">", w - 12)
        txt(x + w / 2, y + 46 + i * 22, ln, f_body)

def arrow(x1, y1, x2, y2, label=None, color=(90, 120, 160)):
    d.line([x1 * S, y1 * S, x2 * S, y2 * S], fill=color, width=2 * S)
    import math
    ang = math.atan2(y2 - y1, x2 - x1)
    L, wd = 9, 4
    p1 = (x2 * S, y2 * S)
    p2 = ((x2 - L * math.cos(ang) + wd * math.sin(ang)) * S, (y2 - L * math.sin(ang) - wd * math.cos(ang)) * S)
    p3 = ((x2 - L * math.cos(ang) - wd * math.sin(ang)) * S, (y2 - L * math.sin(ang) + wd * math.cos(ang)) * S)
    d.polygon([p1, p2, p3], fill=color)
    if label:
        txt((x1 + x2) / 2 + 8, (y1 + y2) / 2, label, f_small, (70, 90, 120))

# 外部世界
box(360, 20, 340, 56, "外 部 世 界")
txt(360 + 340 + 12, 48, "消息 / 系统事件 / 网页抓取", f_small, (70, 90, 120))
arrow(530, 76, 530, 116, "① 落屏")

# 闸门
box(300, 116, 460, 64, "闸 门 ①（无状态：只换格式 · 幂等 · 失败留痕）", fill=(238, 246, 255), outline=(70, 130, 200))
arrow(530, 180, 530, 220, "写 一 次（唯一入口）")

# 账本
box(70, 220, 920, 132, "账 本 · 事 件 流（唯一真相 · append-only · 谁都不改旧行）",
    ["22:14 P5：那个文件叫啥来着    ｜    22:15 鲸鲸：┃回P5：课表_第8周.xlsx",
     "时间 · 会话 · 说者 · 正文 · 字位置 · 引用 · 外部消息号 ＋ 附件内容　——　只增不改"],
    fill=(255, 251, 235), outline=(190, 150, 40), tcolor=(90, 60, 0))
arrow(300, 352, 230, 404, "翻最近 40 条")
arrow(760, 352, 830, 404, "按词 / 意思翻")

# 世界面
box(70, 404, 330, 210, "世 界 面（这一屏）",
    ["22:14 P5：那个文件叫啥来着", "22:15 鲸鲸：┃回P5：课表_第8周.xlsx",
     "40 条 / 2 小时 / 3600 字（超出逐出）", "哑渲染 · 可重建 · 不放判断"],
    fill=(240, 250, 242), outline=(80, 150, 100))

# 记忆立方（同一张原文库，两个来源分区）
box(700, 404, 300, 210, None, fill=(245, 242, 255), outline=(120, 100, 190))
txt(850, 424, "记 忆 立 方（同一张原文库）", f_title, (50, 30, 110))
d.line([850 * S, 444 * S, 850 * S, 596 * S], fill=(120, 100, 190), width=2 * S)
txt(775, 466, "内 部：经历过的", f_body, (50, 30, 110))
for i, s in enumerate(["· 她收到的每一句", "· 她自己说过的", "· 只增不改"]):
    txt(775, 490 + i * 24, s, f_small, (70, 60, 130))
txt(925, 466, "外 部：网上抓的", f_body, (50, 30, 110))
for i, s in enumerate(["· 带 URL 与抓取时间", "· fetch 落成原文行", "· 与内部同构"]):
    txt(925, 490 + i * 24, s, f_small, (70, 60, 130))
txt(850, 586, "索引（倒排 / 向量 / 结构）可丢可重建 · 查不到就返回空", f_small, (70, 60, 130))

# 合流到判
d.line([235 * S, 614 * S, 235 * S, 650 * S], fill=(90, 120, 160), width=2 * S)
d.line([850 * S, 614 * S, 850 * S, 650 * S], fill=(90, 120, 160), width=2 * S)
d.line([235 * S, 650 * S, 850 * S, 650 * S], fill=(90, 120, 160), width=2 * S)
arrow(540, 650, 540, 700, "只读")

# 自我立方
box(40, 700, 320, 170, "自 我 立 方（只被读）",
    ["情绪 valence / arousal（带衰减）", "态度 对每个人 valence / trust", "人设 规范 · 只读 · 版本化"],
    fill=(255, 243, 243), outline=(190, 90, 90), tcolor=(110, 30, 30))

# 判
box(390, 700, 340, 170, "判 ／ 说 ／ 记（唯一有模型的框）",
    ["判：读世界面 + 记忆（按需）+ 自我", "　　→ 复述 → 接 / 不接 / 问", "每个结论带 basis；判不出就弃权"],
    fill=(255, 250, 220), outline=(190, 150, 40), tcolor=(90, 60, 0))
arrow(360, 785, 390, 785, "读")
arrow(730, 785, 800, 785, "说")

# 出口
box(800, 730, 190, 110, "出 口", ["微信 / QQ", "只发不判"], fill=(240, 246, 255), outline=(70, 130, 200))

# 写回
arrow(540, 870, 540, 930, "记")
box(370, 930, 360, 96, "写 回", ["自我立方（只由后果）· 便签（可丢弃）"], fill=(252, 252, 252), outline=(150, 150, 150))

# 纪律
d.rounded_rectangle([40 * S, 1050 * S, 1020 * S, 1102 * S], radius=8 * S, fill=(245, 247, 250), outline=(200, 206, 216), width=2)
txt(530, 1066, "纪律：① 写手只有三个（落屏 / 记 / 摄取）　② 原文永不改写　③ 索引与便签可丢弃", f_legend, (60, 72, 90))
txt(530, 1088, "④ 每条结论都能指回原文　⑤ 三个面之间只有「读」——面与面不许互相写", f_legend, (60, 72, 90))

out = Path("output/design"); out.mkdir(parents=True, exist_ok=True)
p = out / "三面总图.png"
img.save(p)
print("wrote", p, p.stat().st_size, "bytes", img.size)