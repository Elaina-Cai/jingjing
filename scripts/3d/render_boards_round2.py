# -*- coding: utf-8 -*-
"""第二轮板子：留痕条 / 账本 / 感知层 / 记忆三块板 + 候选纸片 / 出口 / 软件适配器
   全部按 viewer_css.html 里的 CSS 尺寸成比例渲染，边框统一内缩 10px（修掉"贴边"）。"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

BG = (10, 14, 22)
TEXT = (228, 234, 242); DIM = (146, 158, 176); ACC = (255, 196, 120)
GOOD = (150, 225, 190); BLUE = (150, 220, 255); PINK = (255, 190, 200)
F = lambda n, s: ImageFont.truetype(rf"C:\Windows\Fonts\{n}", s)

def wrap(d, s, font, maxw):
    out, line = [], ""
    for ch in s:
        if d.textlength(line + ch, font=font) > maxw and line:
            out.append(line); line = ch
        else:
            line += ch
    if line: out.append(line)
    return out

def board(w, h, out, title, sub=None, body=(), footer=None, border=BLUE,
          title_size=None, align="center"):
    img = Image.new("RGB", (w, h), BG); d = ImageDraw.Draw(img)
    d.rounded_rectangle([10, 10, w-10, h-10], radius=18, outline=border, width=3)
    ts = title_size or max(30, int(h * 0.085))
    y = 10 + int(ts * 0.75)
    d.text((w/2, y), title, font=F("msyhbd.ttc", ts), fill=border, anchor="mm")
    y += int(ts * 0.78)
    if sub:
        f_s = F("msyh.ttc", max(16, int(ts * 0.42)))
        d.text((w/2, y), sub, font=f_s, fill=DIM, anchor="mm"); y += int(ts * 0.55)
    d.line([30, y + 6, w-30, y + 6], fill=border, width=2); y += 22
    for kind, txt in body:
        if kind == "h":
            y += 6; d.text((36, y), txt, font=F("msyhbd.ttc", max(17, int(h*0.042))), fill=ACC); y += int(h*0.052)
        elif kind == "t":
            f = F("msyh.ttc", max(16, int(h*0.038)))
            for ln in wrap(d, txt, f, w - 80):
                d.text((44, y), ln, font=f, fill=TEXT); y += int(h*0.046)
        elif kind == "d":
            f = F("msyh.ttc", max(14, int(h*0.032)))
            for ln in wrap(d, txt, f, w - 88):
                d.text((52, y), ln, font=f, fill=DIM); y += int(h*0.040)
        elif kind == "g":
            f = F("msyh.ttc", max(15, int(h*0.034)))
            for ln in wrap(d, txt, f, w - 88):
                d.text((52, y), ln, font=f, fill=GOOD); y += int(h*0.042)
        elif kind == "gap":
            y += int(h*0.018)
    if footer:
        d.line([30, h-58, w-30, h-58], fill=border, width=2)
        f_f = F("msyh.ttc", max(14, int(h*0.030)))
        d.text((w/2, h-32), footer, font=f_f, fill=DIM, anchor="mm")
    p = Path("output/3d")/out; img.save(p)
    print(f"{out:16s} {w}x{h} aspect={w/h:.4f} {p.stat().st_size} bytes")
    return w, h

O = "output/3d"

# 1) 留痕条（CSS 560x130）
board(1120, 260, "t_trace.png", "判决行每回合 · 完整留痕只在开口/动手",
      sub="判决行：turn/chat/ts/decision/basis ｜ 完整留痕：reading/tools/memory",
      body=[("d", "开口或动手才写全：读到了什么、判了什么、凭什么、用了哪个工具"),
            ("d", "沉默只留一行判决 · 不写她的感受，不写模型自述")],
      footer="只增不改 · 可重放", border=(150, 220, 255))

# 2) 账本（CSS 700x200 → 3.5）
board(1120, 320, "t_ledger.png", "账 本 · 事 件 流",
      sub="唯一真相 · append-only · 谁都不改旧行",
      body=[("t", "22:14 P5：那个文件叫啥来着   │  22:15 鲸鲸：┃回P5：课表_第8周.xlsx"),
            ("d", "时间 · 会话 · 说话者 · 正文 · 字位置 · 引用 · 外部消息号  + 附件内容"),
            ("d", "撤回/改口 = 追加新行；读时折叠出「当前状态」")],
      footer="只增不改 · 可重放", border=(255, 210, 150))

# 3) 感知层（CSS 1300x620）
board(1300, 620, "t_perceive.png", "感知层 ①",
      sub="(eventize) · 幂等 · 失败留痕",
      body=[("gap", ""),
            ("d", "只换格式：外部世界 → 一行行事件"),
            ("d", "不改内容、不打标签、不做判断"),
            ("d", "重复投递 = no-op + 计数；失败必落痕，不许静默丢")],
      footer="唯一入口 · 两个时钟（发生 / 入库）· sequence 排序", border=(120, 200, 255), title_size=96)

# 4) 记忆 ① 原文板（CSS 9.6 x 8.58）
board(738, 660, "t_mem_a.png", "① 原 文 板",
      sub="账本的投影，不另存一份",
      body=[("h", "内部：她经历过的"),
            ("d", "· 她收到的每一句、她说的每一句"),
            ("h", "外部：网上抓的"),
            ("d", "· 带 URL 与抓取时间，与内部同构"),
            ("gap", ""), ("d", "改动只能追加：改口 / 撤回都成新行")],
      footer="只增不改 · 指得回原文", border=(255, 210, 150))

# 5) 记忆 ② 索引板
board(738, 660, "t_mem_b.png", "② 索 引 板",
      sub="怎么找到 ① 里的某一行",
      body=[("h", "四种找法"),
            ("d", "· 倒排（词）/ 向量（意思）"),
            ("d", "· 结构（文件·日期·数字·@）"),
            ("d", "· 身份 / 旁注 → 命中行号"),
            ("d", "· 网页 / 群外梗：只作显式 scope"),
            ("gap", ""), ("d", "可丢可重建：删了只是变慢"),
            ("d", "查不到就返回空，不许拿最像的顶")],
      footer="索引不是事实 · 命中必须回到原文", border=(150, 225, 190))

# 6) 记忆 ③ 常驻板
board(738, 660, "t_mem_c.png", "③ 常 驻 板",
      sub="「是不是」这类必须立刻知道的事",
      body=[("h", "装什么"),
            ("d", "· 称呼 / 承诺 / 偏好 / 事实"),
            ("d", "· 每条带：依据 · 写入方式 · 复核日"),
            ("h", "谁能改"),
            ("d", "· 只有人（判只能写候选）"),
            ("gap", ""), ("d", "候选不算记住：转正要过三道门")],
      footer="预算 12 / 200 条 · 候选 7 条（未转正）", border=(255, 190, 200))

# 7) 候选纸片（CSS 240x120）
board(480, 240, "t_mem_cand.png", "候选 7 条",
      sub="未转正", body=[("d", "人批准 / 她明说 / 三次一致"), ("d", "三次须来自不同消息号")],
      footer=None, border=(255, 196, 120), title_size=46)

# 8) 出口（CSS 300x220）—— 清单板
board(900, 660, "t_exit.png", "出  口",
      sub="只放行 · 不判断",
      body=[("d", "① kind 在注册表，且人工已启用"),
            ("d", "② target 存在：会话 / 人 / 自己"),
            ("d", "③ 限流：每分钟 · 每小时 · 每会话"),
            ("d", "④ dry-run 与白名单"),
            ("d", "⑤ 调 adapter，收回执 client_msg_id"),
            ("d", "⑥ 落账：成功失败都写一行"),
            ("gap", ""), ("g", "不挑措辞 · 不补 @ · 不重排 · 不合并")],
      footer="同轮 ≤1 条 say + ≤1 个动作 · 无回执率 0", border=(120, 200, 255), title_size=64)

# 9) 软件 · 适配器（CSS 380x240）
board(950, 600, "t_soft.png", "软 件 · 适 配 器",
      sub="平台差异的唯一发生地",
      body=[("t", "capabilities() → kinds[] · targets · rate_limits · needs_auth"),
            ("d", "一次投递 = 调一个 adapter = 一次平台调用"),
            ("h", "六类错误"),
            ("d", "auth_expired · rate_limited · target_not_found"),
            ("d", "unsupported_kind · network · unknown"),
            ("gap", ""), ("d", "不写世界面、不写记忆：只回执")],
      footer="无回执率必须 0 · 幂等靠 client_msg_id", border=(150, 220, 255), title_size=52)