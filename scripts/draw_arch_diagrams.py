# -*- coding: utf-8 -*-
"""画三张架构全景图（现状 / 目标 / 对照），输出 PNG 到 docs/assets/。

主人原话：「文字理解难度太大，要流程图样式的图、有箭头指向，现状和目标的全景，
只要图就能看懂整个项目状况。」

纪律（照 `docs/全链路架构审计与目标架构.md` 的引用纪律）：
  * 本脚本**不发明任何结论**。每一个框、每一个数字，都能在下面两处找到出处：
      - `docs/全链路架构审计与目标架构.md`（§0.4 两套分层映射表 / §1 现状 38 跳 / §2 目标架构 /
        §3 逐层对照 / §4 乱点 G0–G5）
      - 七份 `docs/架构审计/*.md`（A/B/C/D1/D2/E/F，正文里写 `A §4.2` 这类节号）
  * 每个框的注释里写明它引用的节号 / 乱点编号（见 lanes_fig1 / layers_fig2 / map_rows 的 docstring 与行内注释）。
  * **层名只有一套**（与主稿 §2.2 表一致）：`L0 事实底座 / L1 归属与指代 / L2 N1 意图层 /
    L3 N2 规划-执行 / L4 N3 焦点状态 / L5 N4 规则引擎 / L6 生成与人格 / L7 N5 参数状态机 /
    L8 出站边界 / L9 平台执行` + `L10 记忆层`；**平台入站写作"入站（L9 的对侧）"**。
    图 1（现状）与图 2（目标）逐行同名；旧名（摄取与预处理 / 触发 / 工具层 …）只在图 1 右侧
    「去向」列与图例里出现。改层名前请先改主稿 §0.4「两套分层映射表」。
  * 只用 Pillow，不装新依赖；幂等覆盖输出；中文用 msyh.ttc（微软雅黑）。
  * 逻辑坐标 1x 绘制、scale=2 输出；总宽 2320 / 2040 / 1960 逻辑像素。

框内数字的出处一览（全部来自文档，未新编）：
  38 跳 = 主链 33 + 带 ′ 分支 5          （主稿 §1.3「跳数统计」；判定 23 行 / 管道 15 行）
  92 判定 / 82 处替模型做决定 = 89%      （主稿 §1.4 表首行；C §0.1 给「绕过占 89.1%」）
  全层只有 2 处 LLM 调用                 （主稿 §1.4 第 2 行；C §5 R-A1）
  P0 21 / P1 44 / P2 11 / 去重 76 条      （主稿 §4 末尾「§4 计数」；181 条原始条目）
  匹配 4 / 部分 15 / 不匹配 16（35 行）   （主稿 §3 末尾「匹配度分布」）
  20 块 + 14 类职责 + 595 次 / tool 分支 0 次（D1 §3.4 / §4.1 / §7.2）
  26 个工具 + 条件式注册                  （D1 §3.3 表逐行 +「工具注册表位置」段）
  5 处「缺参数就猜」                      （主稿 §4 G1-9）
  3 处「以为发了其实没发」                （D2 §4-8 send_file / §4-11 表情包面板 / §4-17 msg_id）
  7 条绕过网关直调                        （D2 §6.1② 旁路清单 B1–B7）
  7 处把用户话存成事实                    （E §重点③ 的 7 行表）
  3 表 0 行                               （主稿 §2.3 / §3 #12；B §4.6-5）
  四层无在线纠正                          （E §重点① 末段「纠错能力不对称」）

━━ P0 交付后新增/改动的那几个框（本次刷新，**每个都能核**）━━
  这批徽标只讲**已经落地的交付**，不改上面任何既有数字。计数口径（谁都能数）：
  **P0 已修 5 项 = 主稿 §6 P0「目标」的 5 项 = 5 个交付批（WP1–WP5）**，
  逐项见 `docs/全链路架构审计与目标架构.md` §7「已定决策」#1 / #4 / #7 行
  与 `output/commit_p0_wp1.txt` / `commit_p0_wp24.txt` / `commit_p0_wp3.txt` /
  `commit_p0_wp5.txt` 的提交说明。**图里没有把它们算进/算掉 §4 的 76 条**——
  76 条是主稿自己的计数口径；「√已修」只标「这一框记的乱点，本轮已交付」。
  * WP1（L2）core/contracts/reply_intent.py 551 行 + tests/test_reply_intent_contract.py
    13 用例；flag reply_intent 默认 shadow、**全仓无读者**（= 未接线）→ P0 G2-3 前置
  * WP2（L8）禁语/字数/断言**恒 observe**、重复指纹+沉默熔断保留 → P0 G4-1 / §7 #1#14
  * WP3（L4）db/repo_focus.py + 迁移 54 建 session_focus（每会话一行，**表已建 0 行**）
    → §7 #4；聚类内核 threads.py 与消费方注入属 P1
  * WP3/P0-WP4（L0）`CoarseAssigner` 整类删（relevance.py 608→316 行；话题守卫
    tests/test_topic_no_persist_guard.py）→ P0 G2-2；「三表 0 行」这条随之消掉
  * P0-WP4（L0）第二 INSERT 写方收编回 owner：core/tasks/store.py 改调
    `append_in_session`，三道保护**同一份实现**，白名单收缩为 1 个文件 → P0 G2-1
  * P0-WP4（L0 底座）5 个恒真开关 → 三态（flag 名集中注册在 core/features.py）
    → P0 G4-2 / §3 #32
  * WP5（L10 + 入站 + L9 Web）记忆范围**行级 scope 强制 + fail-closed**
    （tests/test_memory_scope.py 8 用例）；`/media/*` 纳入鉴权前缀；
    `/api/local-file` 与 `/media/image` 根约束 + 扩展名白名单；管理面伪造 owner
    → 审计 + 非本机 403（tests/test_web_security.py 6 用例）→ P0 G3-2 / G5-10/11/12
  * **仍未落地（图上照旧标红/P0）**：P0 G2-3 关键词意图降级为证据（与 N1 同批 P1）、
    G2-9 改调 speaker owner、G2-12 问句判定收编、G1-9/G1-13/G4-2 其余各项、
    G5-15 LLM 结构化输出校验
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# 路径 / 输出
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "docs" / "assets"
SCALE = 2  # 逻辑坐标 1x 绘制，2x 输出（清晰）

# ---------------------------------------------------------------------------
# 中文字体（硬性要求：msyh.ttc → msyhbd.ttc → simhei.ttf）
# ---------------------------------------------------------------------------
FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
]


def pick_font_path() -> Path:
    for p in FONT_CANDIDATES:
        if p.is_file():
            return p
    raise SystemExit(f"找不到任何中文字体：{[str(p) for p in FONT_CANDIDATES]}")


FONT_PATH = pick_font_path()
_FONT_CACHE: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """msyh.ttc 是集合字体，bold 用内部 face index=1；取不到就退回同文件。"""
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    index = 1 if (bold and FONT_PATH.name == "msyh.ttc") else 0
    try:
        f = ImageFont.truetype(str(FONT_PATH), size, index=index)
    except Exception:
        f = ImageFont.truetype(str(FONT_PATH), size)
    _FONT_CACHE[key] = f
    return f


# ---------------------------------------------------------------------------
# 配色（统一 + 每张图自带图例）
#   绿 = 已达标 / 已收口　　黄 = 部分达标（需收编）　　红 = P0 乱点
#   灰 = 死代码 / 退役　　　蓝 = 新增结构　　　　　　 箭头 = 深灰实线，主干自上而下
# ---------------------------------------------------------------------------
INK = "#1B2430"
INK_SOFT = "#3C4654"
INK_MUTED = "#5B6674"
LINE = "#E3E8EE"
PAGE = "#FFFFFF"

C_MATCH = "#2E9E63"  # 绿
C_PART = "#E8A317"  # 黄
C_P0 = "#D64545"  # 红
C_DEAD = "#98A2AE"  # 灰
C_NEW = "#2C6FD1"  # 蓝
C_FIXED = "#0E8C7F"  # 青绿：P0 已修（收尾轮新增的一格，见 §0.4 图例表）
C_ARROW = "#4A5462"  # 深灰

# 图 1 专用：现状特有阶段名的「去向」注记色（与主干箭头深灰、回流箭头区分）
C_TO_L0 = "#1F7A8C"   # 青：摄取与预处理 → 并入 L0
C_TO_L2 = "#8A5CD1"   # 紫：触发 → 并入 L2 / 决策
C_TO_L3 = "#D2691E"   # 橙：工具层 → 并入 L3 内层

FILLS = {
    C_MATCH: "#E6F5EC",
    C_PART: "#FDF3DC",
    C_P0: "#FCE9E9",
    C_DEAD: "#F0F2F5",
    C_NEW: "#E8F1FD",
    C_FIXED: "#E1F4F1",
    C_ARROW: "#F4F6F9",
    C_TO_L0: "#E4F4F6",
    C_TO_L2: "#F1EAFB",
    C_TO_L3: "#FBEDE0",
}

LEGEND_LABELS = [
    (C_MATCH, "绿：已达标 / 已收口"),
    (C_PART, "黄：部分达标，需收编"),
    (C_P0, "红：P0 乱点"),
    # 「√已修（P0）」= 本轮 P0 交付**已落地**、图上不再算乱点的那些框。
    # 计数口径：主稿 §6 P0「目标」5 项 ⇄ 5 个交付批（WP1–WP5），逐项可在
    # docs/全链路架构审计与目标架构.md §7「已定决策」#1/#4/#7 行与
    # output/commit_p0_wp*.txt 的提交说明里核出来。**不改任何既有数字**。
    (C_FIXED, "√已修（P0）：P0 交付已落地（徽标「√已修」，不再算乱点）"),
    (C_DEAD, "灰：死代码 / 退役 / （图 1）现状缺失结构"),
    (C_NEW, "蓝：新增结构"),
]
ARROW_LEGEND = "深灰实线箭头 ＝ 主干流向（自上而下）"
# 图 1 追加图例：现状特有阶段名的去向（与主干箭头、回流箭头颜色都不同）
TO_LEGEND_FIG1 = [
    (C_TO_L0, "青箭头＝摄取与预处理 → 并入 L0"),
    (C_TO_L2, "紫箭头＝触发 → 并入 L2／决策"),
    (C_TO_L3, "橙箭头＝工具层 → 并入 L3 内层"),
]

#: 图上所有徽标共用的一个数字（必须在本文档里数得出来）：本轮 P0 交付**全部落地**
#: 的批数 = 主稿 §6 P0「目标」的 5 项 = WP1 意图层契约 / WP2 闸门只观测 /
#: WP3 焦点状态落一行 / WP4 死代码与恒真开关 / WP5 安全与不变量硬门槛。
P0_FIXED_BADGE = "P0 已修 5 项"

#: 图里真正用到的**非 ASCII 记号**——自检按位图确认它们不是 .notdef 豆腐块
#: （`✓` U+2713 在 msyh.ttc 里缺字形，渲出来是方框；`√` U+221A 有字形）。
SIGIL_PROBE = ("√", "─", "→", "｜", "·", "×")


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
def text_w(d: ImageDraw.ImageDraw, s: str, f: ImageFont.FreeTypeFont) -> float:
    return d.textlength(s, font=f)


def wrap(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_w: float,
         indent: str = "") -> list[str]:
    """按可用宽度折行（CJK 友好：逐字贪心；不让标点落在行首，必要时压缩间距）。"""
    NO_HEAD = "，。；：！？、）】》”’%"
    out: list[str] = []
    for raw in text.split("\n"):
        cur = indent
        for ch in raw:
            if text_w(d, cur + ch, f) <= max_w or not cur.strip():
                cur += ch
                continue
            # 放不下：判断能否把最后一个字挪到下一行
            if ch not in NO_HEAD and text_w(d, cur, f) <= max_w * 1.02:
                out.append(cur.rstrip())
                cur = indent + ch
            else:
                out.append(cur.rstrip())
                cur = indent + ch
        out.append(cur.rstrip())
    return [ln for ln in out] or [""]


# ---------------------------------------------------------------------------
# 图元
# ---------------------------------------------------------------------------
def arrow_down(d: ImageDraw.ImageDraw, x: float, y0: float, y1: float,
               color: str = C_ARROW, w: int = 3, head: int = 12) -> None:
    d.line([(x, y0), (x, y1 - head + 2)], fill=color, width=w)
    d.polygon([(x, y1), (x - head * 0.62, y1 - head * 1.35), (x + head * 0.62, y1 - head * 1.35)],
              fill=color)


def _dashed_line(d: ImageDraw.ImageDraw, pts: list[tuple[float, float]], color: str,
                 w: int, dash: float = 10, gap: float = 7) -> None:
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        dist = math.hypot(x1 - x0, y1 - y0)
        if dist <= 0:
            continue
        ux, uy = (x1 - x0) / dist, (y1 - y0) / dist
        t = 0.0
        while t < dist:
            e = min(t + dash, dist)
            d.line([(x0 + ux * t, y0 + uy * t), (x0 + ux * e, y0 + uy * e)], fill=color, width=w)
            t = e + gap


def dashed_rrect(d: ImageDraw.ImageDraw, box, radius: float, color: str, w: int = 2,
                 dash: float = 9, gap: float = 6) -> None:
    x0, y0, x1, y1 = box
    r = radius
    _dashed_line(d, [(x0 + r, y0), (x1 - r, y0)], color, w, dash, gap)
    _dashed_line(d, [(x1, y0 + r), (x1, y1 - r)], color, w, dash, gap)
    _dashed_line(d, [(x1 - r, y1), (x0 + r, y1)], color, w, dash, gap)
    _dashed_line(d, [(x0, y1 - r), (x0, y0 + r)], color, w, dash, gap)
    d.arc([x0, y0, x0 + 2 * r, y0 + 2 * r], 180, 270, fill=color, width=w)
    d.arc([x1 - 2 * r, y0, x1, y0 + 2 * r], 270, 360, fill=color, width=w)
    d.arc([x1 - 2 * r, y1 - 2 * r, x1, y1], 0, 90, fill=color, width=w)
    d.arc([x0, y1 - 2 * r, x0 + 2 * r, y1], 90, 180, fill=color, width=w)


def poly_arrow(d: ImageDraw.ImageDraw, pts: list[tuple[float, float]], color: str = C_ARROW,
               w: int = 3, head: int = 13, dashed: bool = False) -> None:
    """折线 + 末端箭头（按最后一段方向定向）。"""
    pts = list(pts)
    if len(pts) < 2:
        return
    if dashed:
        _dashed_line(d, pts, color, w, dash=9, gap=6)
    else:
        for a, b in zip(pts, pts[1:]):
            d.line([a, b], fill=color, width=w, joint="curve")
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    ang = math.atan2(y1 - y0, x1 - x0)
    a1, a2 = ang + math.radians(152), ang - math.radians(152)
    d.polygon([(x1, y1),
               (x1 + head * math.cos(a1), y1 + head * math.sin(a1)),
               (x1 + head * math.cos(a2), y1 + head * math.sin(a2))], fill=color)


def legend(d: ImageDraw.ImageDraw, x: float, y: float, items: list[tuple[str, str]],
           max_w: float, size: int = 19, gap_x: float = 34, gap_y: float = 14,
           arrow_note: str | None = None) -> float:
    """一行/多行图例。items = [(颜色, 文案), ...]；返回占用的高度。"""
    f = font(size)
    fb = font(size + 1, bold=True)
    cx, cy = x, y
    row_h = size + 14
    for color, label in items:
        w = text_w(d, label, f) + size + 26
        if cx > x and cx + w > x + max_w:
            cx, cy = x, cy + row_h + gap_y
        d.ellipse([cx, cy - size / 2, cx + size, cy + size / 2], fill=color)
        d.text((cx + size + 10, cy), label, font=f, fill=INK_SOFT, anchor="lm")
        cx += w + gap_x
    cy += row_h
    if arrow_note:
        d.line([(x, cy - 6), (x + 74, cy - 6)], fill=C_ARROW, width=3)
        d.polygon([(x + 82, cy - 6), (x + 70, cy - 13), (x + 70, cy + 1)], fill=C_ARROW)
        d.text((x + 92, cy - 6), arrow_note, font=fb, fill=INK_SOFT, anchor="lm")
        cy += row_h
    return cy - y


# ---------------------------------------------------------------------------
# 框 / 泳道
# ---------------------------------------------------------------------------
@dataclass
class Node:
    key: str
    color: str
    title: str
    body: str
    badge_text: str = ""   # 框顶左侧的彩色徽标（如 "P0"）
    dashed: bool = False


@dataclass
class Lane:
    num: str
    name: str
    color: str
    hint: str
    nodes: list[Node] = field(default_factory=list)
    dashed: bool = False
    # 图 1 专用：现状特有阶段名的「去向」注记（写入右侧去向列）。
    # merge = (旧阶段名, 去向说明, 箭头色)；None = 本行没有需要交代的旧阶段名。
    merge: tuple[str, str, str] | None = None


class Canvas:
    """逻辑坐标 1x 绘制、SCALE 倍输出。

    绘制面按 **1x 逻辑像素**建立，所有坐标都是逻辑像素；出图时整体
    nearest 放大 SCALE 倍 —— 这样"画"与"算"在同一坐标系里，不会出现
    高度算成 2 倍、内容却按 1 倍画的情况。
    """

    def __init__(self, w: float, h: float) -> None:
        self.img = Image.new("RGB", (int(round(w)), int(round(h))), PAGE)
        self.d = ImageDraw.Draw(self.img)
        try:
            self.d.fontmode = "L"  # 抗锯齿
        except Exception:
            pass
        self.w, self.h = w, h

    def save(self, path: Path) -> tuple[int, int, int]:
        path.parent.mkdir(parents=True, exist_ok=True)
        out = self.img
        if SCALE != 1:
            out = out.resize((out.width * SCALE, out.height * SCALE), Image.NEAREST)
        out.save(path, "PNG", optimize=True)
        w, h = out.size
        return w, h, path.stat().st_size


# ---------------------------------------------------------------------------
# 布局参数
# ---------------------------------------------------------------------------
M = 40            # 页边距
LABEL_W = 208     # 泳道标签列宽
LBL_GAP = 24
FB_W = 300        # 图 1 右侧「回流 + 旧阶段名去向」列宽
BOX_GAP = 16
ROW_GAP = 12
HEAD_H = 126      # 标题栏高度下限（**实际高度由 title_block 按徽标行数算**；本值只当量测画布的高度提示）
PROBE_H = 4000    # 第一遍"只量不画"的临时画布高度：只要够大就行（画布不会再被保存）

W1 = 2320
W2 = 2040
W3 = 1960
MAX_PER_ROW = 5   # 一行最多 5 个框，超过就折成两行


def badge_width(d: ImageDraw.ImageDraw, text: str, size: int = 21) -> float:
    return text_w(d, text, font(size, bold=True)) + 30


def draw_badges(c: Canvas, badges: list[tuple[str, str]], right: float,
                top: float, x_limit: float, rows: int, size: int = 21) -> None:
    """从右往左排计数徽标，**放不下就换到下一行**（行数由 :func:`badge_rows` 先算好）。"""
    d = c.d
    fb = font(size, bold=True)
    bx, by, used = right, top, 1
    for text, color in reversed(badges):
        w = badge_width(d, text, size)
        if bx - w < x_limit and bx < right:      # 本行放不下 → 换行（首个不换）
            bx, by, used = right, by + 46, used + 1
            if used > rows:
                raise SystemExit(f"徽标排不下（超过 {rows} 行）：{[t for t, _ in badges]}")
        d.rounded_rectangle([bx - w, by, bx, by + 44], radius=9,
                            fill=FILLS.get(color, PAGE), outline=color, width=2)
        d.text(((bx - w + bx) / 2, by + 22), text, font=fb, fill=color, anchor="mm")
        bx -= w + 12


def badge_rows(c: Canvas, badges: list[tuple[str, str]], width: float,
               x_limit: float, size: int = 21) -> int:
    """徽标要占几行（与 :func:`draw_badges` 同一套换行规则；只测量不绘制）。"""
    d = c.d
    bx, rows = width - M, 1
    for text, _ in reversed(badges):
        w = badge_width(d, text, size)
        if bx - w < x_limit and bx < width - M:
            rows += 1
            bx = width - M
        bx -= w + 12
    return rows


def title_block(c: Canvas, title: str, subtitle: str,
                badges: list[tuple[str, str]], width: float) -> float:
    """标题栏 = **标题带**（标题 + 一句话副标题）+ **徽标带**（计数徽标，右对齐）；返回下一个可用 y。

    为什么分两条带（不是原来的一行挤）：图 1/图 2 的标题本身就有 849 / 811 逻辑像素宽，
    再加一个「P0 已修 5 项」徽标就和标题**必然重叠**（原实现就是右对齐直接压上去）。
    分带后：标题与副标题独占第一行、徽标在第二行右对齐，加多少个徽标都不会压到文字。
    徽标一行放不下时 :func:`badge_rows` 会自动折行，标题栏高度随之长高。
    """
    d = c.d
    rows = badge_rows(c, badges, width, M + 120)
    head_h = 96 + 54 * rows - 4          # 标题带 92 + 每条徽标带 54 + 下边距
    d.rectangle([0, 0, width, head_h], fill="#F4F7FB")
    d.line([(0, head_h), (width, head_h)], fill=LINE, width=2)
    d.rectangle([0, 0, 9, head_h], fill=C_NEW)
    d.text((M, 44), title, font=font(38, bold=True), fill=INK, anchor="lm")
    d.text((M, 92), subtitle, font=font(21, bold=True), fill=C_P0, anchor="lm")
    d.line([(M, 92 + 16), (width - M, 92 + 16)], fill=LINE, width=2)
    draw_badges(c, badges, width - M, 108, M + 120, rows)
    return head_h + 24


def footer(c: Canvas, y: float, width: float, text: str) -> None:
    d = c.d
    d.line([(M, y), (width - M, y)], fill=LINE, width=2)
    for i, ln in enumerate(wrap(d, text, font(17), width - 2 * M)):
        d.text((M, y + 22 + i * 25), ln, font=font(17), fill=INK_MUTED, anchor="lm")


def _lane_caption(d: ImageDraw.ImageDraw, lane: Lane, x0: float, x1: float, cy: float,
                  fill: str) -> None:
    """泳道标签胶囊里的文字：编号 + 名称 + 一行现状提示。

    提示行会自动折行并**收在胶囊内**（超宽就自动降一档字号），不再溢出到框上。
    """
    cx = (x0 + x1) / 2
    inner = x1 - x0 - 16
    d.text((cx, cy - 20), lane.num, font=font(38, bold=True), fill=fill, anchor="mm")
    nl = wrap(d, lane.name, font(19, bold=True), inner)
    for i, ln in enumerate(nl):
        d.text((cx, cy + 16 + i * 24), ln, font=font(19, bold=True), fill=fill, anchor="mm")
    hy = cy + 20 + len(nl) * 24
    for size in (14, 13, 12):
        hl = wrap(d, lane.hint, font(size), inner)
        if len(hl) <= 3:
            break
    for i, ln in enumerate(hl):
        d.text((cx, hy + i * (size + 4)), ln, font=font(size), fill=fill, anchor="mm")


def lane_rows(n: int) -> list[int]:
    """把 n 个框分到 1–2 行（≥6 个折两行，避免框太窄）。"""
    if n >= 6:
        half = (n + 1) // 2
        return [half, n - half]
    return [n]


# ===========================================================================
# 图 1 —— 现状架构全景（11 行 = 目标层名 L0–L10 + 平台入站，压缩自 §1 的 38 跳）
#
# ★ 层名口径（与图 2 完全同一套）：行名 = 目标架构层名（§2.2 表为准）。
#   旧版图 1 用的是「代码流水线阶段名」（平台入站 / 摄取与预处理 / 事实层 /
#   触发层 / 理解与准入 / 生成层 / 工具层 / 出站层 / 记忆与人物回流），
#   与图 2 的 L0–L10 是两套名字，看起来像"少了几层"。现按 §0.4「两套分层
#   映射表」逐行对齐；旧阶段名的去向由 Lane.merge 注记在右侧去向列里。
# ★ 新增结构那几行照任务书写法：**灰色虚线「现状缺失」行**，框内一句现状。
# ===========================================================================
def lanes_fig1() -> list[Lane]:
    """每个框的注释 = 出处（主稿节号 / 乱点编号 / 分区审计节号）。

    行序 = 入站对侧（L9 的对侧）→ L0 事实底座 → … → L9 平台执行 → L10 记忆层，
    与图 2（目标架构全景）逐行同序同名的 11 行，便于左右对照。
    """
    return [
        # ── 入站（L9 的对侧）：旧泳道「平台入站」（§1.1 跳 1–2） ──────────
        Lane("入站", "平台入站（L9 的对侧）", C_PART,
             "§1.1 跳 1–2｜L9 的对侧：入站有第二条路径", [
            Node("wechat", C_P0, "微信 DB 文件监听",
                 "跳 1｜db_watcher.py:16 脏标记…疑无生产构造点\n第二条入站触发路径，无 flag 无审计 → 退役或登记\n"
                 "P0 G2-3 同域｜A §4.20/4.21", "P0"),
            Node("qq", C_PART, "QQ provider 流",
                 "跳 1｜QQ 走 provider 异步流 → 主循环（WxMessage / InboundMessage）\n"
                 "A §1 跳 1｜D2 §6.1④ QQ 侧可作样板", "部分"),
            Node("web", C_FIXED, "Web 接口（鉴权覆盖面已修）",
                 "§3 #34 部分｜/media/* 已纳入鉴权前缀：GUARDED_PREFIXES = /api/ · /ws · /media/\n"
                 "（core/web app.py:25；过去 /media/* 在覆盖外，是私聊图片/头像/表情包）\n"
                 "WEB_TOKEN 空 = 每次进程打印一条 web_token_not_configured（不静默）\n"
                 "任意路径读文件已修（P0 G5-10，见 L9 Web 管理面）｜F §4 F22/F27｜P0 G5-11", "√已修"),
        ]),
        # ── L0 事实底座（旧泳道「摄取与预处理」跳 2–3 + 「事实层」跳 4–7） ──
        Lane("L0", "事实底座", C_P0,
             "§2.2 L0｜只产事实与证据；第二 INSERT 写方已收编回 owner、CoarseAssigner 已删", [
            Node("collect", C_PART, "采集器扫描落库（管道）",
                 "跳 2｜跨线程去重闸门 → 平台采集器扫描 → db.models.Message 行\n"
                 "scheduler/main.py:93-130｜platform_messages.py:253｜A §1", "部分"),
            Node("outbox", C_PART, "同事务登记 outbox + 媒体（跨库写）",
                 "跳 3｜event_outbox 行 / message_media 行｜管道（有静默失败，B:412）\n"
                 "B §4.5｜D2 §6.1②：event_outbox 是入站队列，不是发送账", "部分"),
            Node("coarse_gone", C_FIXED, "CoarseAssigner 已删（P0 G2-2 交付）",
                 "原 281 行、零调用方，却写 threads / thread_msg_ref（三表 0 行）\n"
                 "→ 整类 + commit_assignments 已从 core/preprocess/relevance.py 删除（608→316 行）\n"
                 "「三表 0 行」这条乱点随之消掉（没有写方了）；\n"
                 "话题聚类不许落库的静态守卫 = tests/test_topic_no_persist_guard.py\n"
                 "§7 #7 已落地｜P0 G2-2｜A §4.2｜B §4.6-5", "√已修"),
            Node("kw", C_P0, "关键词表写进事实行",
                 "跳 4｜把入站文本判成 command/request/question/message，烧进 event_type + metadata.intent\n"
                 "P0 G2-3｜A §4.3｜R-A1 硬条款：只产证据，不得下结论", "P0"),
            Node("ev2", C_FIXED, "第二条 INSERT 写方已收编（P0 G2-1）",
                 "跳 5′｜core/tasks/store.py 不再自建写法，改调 owner 的 append_in_session\n"
                 "→ 三道保护（幂等提前返回 / 四元组回查 / RETURNING sequence 非空校验）\n"
                 "是 owner 里的同一份实现，不再各写一份\n"
                 "白名单收缩为 1 个文件 + 重放场景用例｜P0 G2-1｜§7 #7｜A §4.1｜B §4.6-3", "√已修"),
            Node("ev1", C_MATCH, "owner 唯一 INSERT（已收口）",
                 "跳 5｜库内原子取号 + 四元组幂等 + 失败不静默｜terminal_events.sequence 单调\n"
                 "绿：A §5.1「17 条已做对」｜§2.2 L0 保留", "已达标"),
            Node("list4", C_P0, "消息类清单一概念 4 份",
                 "含集 ×2 / 排除集 ×2：新增一个 EventType 要同步改 4 处\n"
                 "P1 G2-11｜A §4.5｜event_model.py:32-34 注释记过一次静默降级事故", "P1"),
        ], merge=("摄取与预处理", "并入 L0", C_TO_L0)),
        # ── L1 归属与指代（旧泳道「理解与准入」的前半，§1.2 跳 11/13/14） ──
        Lane("L1", "归属与指代", C_MATCH, "§2.2 L1｜R-B1/R-T3 已满足，守卫绿", [
            Node("world", C_MATCH, "窗口读 → 同源派多块事实",
                 "跳 11｜一次窗口读，同源派生多块事实（每块 sha12 可对账）\n"
                 "绿：C 区最扎实的一半（C:258）｜world_state.py:139-243", "已达标"),
            Node("attr", C_MATCH, "归属边 + 指代绑定（已达标）",
                 "跳 13/14｜addressing.py:623 resolve_attribution（边）｜referent.py:220 bind_pronouns（指代）\n"
                 "绿：一处判一处用没有漂｜§2.2 L1｜R-B1/R-T3 已满足", "已达标"),
            Node("room8", C_P0, "4 条正则下 8 类意图结论",
                 "room_state.py:385-451｜位置规则给别人的动机下 8 类结论（结论注入）\n"
                 "P0 G1-1｜C §2 A22/A27｜姿态 6 值 ≠ 契约 9 值（G1-2）→ 目标落在 L2 态度值", "P0"),
        ]),
        # ── L2 N1 意图层（目标新增；现状：契约已建成但**未接线**）──────────
        Lane("L2", "N1 意图层", C_DEAD,
             "§2.2 L2｜目标：大 LLM 唯一 owner；现状：契约已建成、判定仍分散、未通电", [
            Node("miss_l2", C_DEAD,
                 "现状缺失：N1 owner 未通电，判定分散在 5 道闸门 / 7 份问句词表 / 7 组关键词里",
                 "92 判定点里 82 处绕过 = 89%；主判据 100% 是关键词 / 正则 / 阈值 / 计数\n"
                 "全层只有 2 处 LLM 调用 = admission.py:303 + topic_line.py:168；\n"
                 "TurnIntent 产出 6 值、下游却忽略形参（空转）\n"
                 "→ 判定没有被 N1 收编，契约建成不等于通电\n"
                 "主稿 §1.4｜C §0.1 / §4-10 / §5", "现状缺失"),
            Node("n1contract", C_FIXED, "N1 意图层契约已建成（P0-WP1 交付）",
                 "core/contracts/reply_intent.py｜intent 6 值 + attitude 9 值封闭集合、\n"
                 "置信度三条腿（候选概率 / 结构化证据 / 保守合成取低者）、need_clarify 三条件真值表\n"
                 "纯结构：不调 LLM / 不读库 / 不接路由 → 新增 LLM 调用 = 0\n"
                 "flag reply_intent 默认 shadow（features.REPLY_INTENT，全仓无读者）\n"
                 "接线（谁读这份结论）属 P1｜§7 #1｜P0 G2-3 前置｜R-A1..A-8", "√已修"),
            Node("switch", C_PART, "唯一回复总开关",
                 "跳 8｜config.persona.enabled（不是 feature flag）；关掉整链静默不跑，/api/status 也不显示\n"
                 "§1.1 跳 8｜F:132-134｜默认 False core/config/settings.py:125", "部分"),
            Node("route", C_PART, "路由 / 发言模式解析",
                 "跳 9｜TurnDecision{action, reason}｜规则表 policy_rules\n"
                 "core/speech/resolver.py:558-648（owner）→ router.py:318/366-368", "部分"),
            Node("trig5", C_PART, "群缓冲 → 触发类型（IMMEDIATE/DEFERRED/AMBIENT）",
                 "跳 10｜group_state.py:109-144 → router.py:643-644/1247\n"
                 "F:76｜群聊非点名默认 DEFER 15s", "部分"),
            Node("gates5", C_P0, "5 道并列闸门（默认态互斥）",
                 "跳 12｜一票否决，模型没有第二票；声明「两票制」实测是一票否决\n"
                 "默认开：关键词闸门；默认关：结构减法与社交事实（21 个 flag 速查）\nP0 G2-4/G4-3｜C §4-2/4-12/4-13", "P0"),
            Node("q7", C_P0, "「是不是问句」7 份实现",
                 "词表/正则各不同（reconcile.py:56 只判 2 个字符）\n"
                 "→ 收编到 addressing.QUESTION_MARKERS\nP0 G2-12｜C §4-3｜A §4.11", "P0"),
            Node("turnintent", C_P0, "TurnIntent 空转",
                 "产出 6 值但下游忽略形参（build_turn_intent_hint 只看 unknown_answer）\n"
                 "→ 对行为零影响｜P0 G1-6｜C §4-10", "P0"),
            Node("no_adm", C_P0, "触发路径完全不过准入",
                 "跳 12′｜router.py:1424-1427 注释逐字「不花这次 judge」；唯一准入点 router.py:930\n"
                 "P0 缺口 G7 守卫不存在｜docs/架构所有权地图.md:191", "P0"),
        ], merge=("触发", "并入 L2／决策", C_TO_L2)),
        # ── L3 N2 规划-执行（目标新增；现状：无外层）──────────────────────
        Lane("L3", "N2 规划-执行", C_DEAD,
             "§2.2 L3｜目标：外层步骤 + 内层对比；现状：无外层，日常回合进不去", [
            Node("miss_l3", C_DEAD,
                 "现状缺失：无 N2 外层（planner 有顺序、缺三字段），内层 verify_step 一次未进主循环",
                 "SubTask 十字段缺 needed_info / expected_output / success_criteria 三字段；\n"
                 "日常回合进不去（daily chat must never enter Planner）；\n"
                 "_verify_step 只判 2 条（空召回 / 搜索范围不符）且原生循环一次都没调\n"
                 "§3 #16｜§2.2 L3｜D1 §7.4｜R-C1/C2/C4/C5 缺失", "现状缺失"),
            Node("assembler", C_PART, "assembler 20 块 + 块策略",
                 "persona_brain 生产 20 个块；assembler._BLOCK_POLICY 是块清单唯一登记处\n"
                 "（26 条前缀映射）；块清单另有 3 处来源（D1:488）｜D1 §3.4", "部分"),
            Node("conv3024", C_PART, "conversational 14 类职责 / 3024 行",
                 "一个文件压 14 类职责：4 套并行 prompt 拼装路径\n"
                 "（两函数 78 行逐字相同）｜D1 §4.1", "部分"),
            Node("fastpath", C_P0, "fast_path 三条确定性路径从未触发",
                 "7 组关键词/正则分流；实测 595 次判定里 tool 分支 0 次\n"
                 "P0 G1-10｜D1 §7.2｜与 R-A1 正面冲突", "P0"),
            Node("tools26", C_PART, "26 个工具表（D1 §3.3 逐行）",
                 "统一构造点 ToolRegistry.create_default()；注册是环境条件式的\n"
                 "（vector_store / event_bus / send_enabled / platform / flag 五个条件）\n"
                 "→ 同一模型在不同机器看到不同工具集｜约 24 个 schema ≈ 11.8k 字符进 prompt", "部分"),
            Node("actpre", C_P0, "动作前置 12 处 / 5 处在工具内",
                 "12 处校验点，其中 5 处工具内部各写一份「这个人能不能干这件事」\n"
                 "P0 G1-13｜F §3.5｜规则 conditions 不参与放行；max_calls/require_confirmation 零读取方", "P0"),
            Node("guess5", C_P0, "5 处「缺参数就猜」",
                 "最长名优先猜人 / 让 LLM 发明选项并打分 / 未知事件类型静默改成别的\n"
                 "/ 名字取第一个匹配 / 缺参取默认值｜P0 G1-9｜D1 §4.1 清单", "P0"),
        ], merge=("工具层", "并入 L3 内层", C_TO_L3)),
        # ── L4 N3 焦点状态（状态载体已建；判定与接线属 P1）─────────────
        # 口径（P0-WP3 之后，**按批分开说，不许合成一句话**）：
        #   * 落库那一行（session_focus 单表）已建 → 绿标「√已修」；
        #   * 聚类内核 + 消费方接线**仍是 P1**，所以本行的 hint 仍写"判定未通电"；
        #   * 原「无 owner」判据之一（CoarseAssigner）**已删**，不能再当现状乱点讲。
        Lane("L4", "N3 焦点状态", C_DEAD,
             "§2.2 L4｜状态载体已建（session_focus 单表，0 行）；判定未通电，聚类与注入属 P1", [
            Node("miss_l4", C_DEAD,
                 "现状缺失：焦点状态判定无 owner——由话题簇 3 组近似实现 + room_state 聚类顶替",
                 "话题簇 3 组近似实现互不共享边界；core/context/threads.py 实测不存在；\n"
                 "原 CoarseAssigner 这条路已随 P0-WP3 删除（不再是现状乱点）；\n"
                 "session_focus 只是状态载体，聚类内核与消费方接线都还没来\n"
                 "§2.2 L4｜§3 #12｜P0 G2-2 已交付那半｜R-T5/T1", "现状缺失"),
            Node("focus_row", C_FIXED, "焦点状态落一行已建（P0-WP3 交付）",
                 "db/repo_focus.py + 迁移 54（db/migrations.py:2051）建 session_focus：\n"
                 "每会话一行，主键 (platform, account_id, chat_id)，12 列与 meta_models\n"
                 "SessionFocusState 同形（用例做 PRAGMA 对照）\n"
                 "表已建、0 行：聚类内核 core/context/threads.py 与消费方注入属 P1；\n"
                 "降级契约写死：丢行只损失「一句确认」，读失败返回 None 不抛异常\n"
                 "§7 #4 已落地（生产库未执行迁移）｜R-T5/§2.2 L4｜P0 G2-2", "√已修"),
            Node("dead3", C_P0, "3 表 0 行 + 话题 owner 不存在",
                 "topics / threads / thread_msg_ref 创建路径不可达、实测 0 行\n"
                 "core/context/threads.py 实测不存在；话题簇 3 组近似实现互不共享边界\n"
                 "P0 G2-2 / §3 #12｜B §4.6-5｜先答「两套 Thread 是不是同一件事」（§7 #4）", "P0"),
        ]),
        # ── L5 N4 规则引擎（目标新增；现状：无）──────────────────────────
        Lane("L5", "N4 规则引擎", C_DEAD,
             "§2.2 L5｜目标：五维规则 + Web UI 可配；现状：无统一判定点", [
            Node("miss_l5", C_DEAD,
                 "现状缺失：N4 不存在，「执行动作所需条件」散在 21 个 flag 与硬编码阈值里",
                 "core/rules/engine.py 不存在：五维里 target / quota / action / explanation 缺 4 样；\n"
                 "主动开口阈值三本账、NON_OWNER_PENDING_CAP=5 常数、动作前置 12 处各写一份\n"
                 "§2.2 L5｜§3 #27/#28｜P0 G1-13｜F §3.3/§3.5｜R-V2", "现状缺失"),
        ], dashed=True),
        # ── L6 生成与人格（旧泳道「生成层」，§1.3 跳 23–26） ──────────────
        Lane("L6", "生成与人格", C_PART,
             "§2.2 L6｜骨架保留（方案 A：用新层包裹，不拆 conversational.py）", [
            Node("assembler", C_PART, "assembler 20 块 + 块策略",
                 "persona_brain 生产 20 个块；assembler._BLOCK_POLICY 是块清单唯一登记处\n"
                 "（26 条前缀映射）｜D1 §3.4｜块清单另有 3 处来源（D1:488）", "部分"),
            Node("conv3024", C_PART, "conversational 14 类职责 / 3024 行",
                 "一个文件压 14 类职责：4 套并行 prompt 拼装路径\n"
                 "（两函数 78 行逐字相同）｜D1 §4.1", "部分"),
            Node("fastpath", C_P0, "fast_path 三条确定性路径从未触发",
                 "7 组关键词/正则分流；实测 595 次判定里 tool 分支 0 次\n"
                 "P0 G1-10｜D1 §7.2｜与 R-A1 正面冲突", "P0"),
        ]),
        # ── L7 N5 参数状态机（目标新增；现状：不存在）────────────────────
        Lane("L7", "N5 参数状态机", C_DEAD,
             "§2.2 L7｜目标：GATHERING→…→DONE/ABORTED；现状：不存在", [
            Node("miss_l7", C_DEAD,
                 "现状缺失：无参数状态机，只有一次「我准备这样做…可以继续吗？」",
                 "R-P1..P8 全套缺失；无 pending 状态、无引用校验、把下一句话当确认\n"
                 "现状代偿物：agents/task_session.py（69 行内存态）、core/tasks/store.py 台账、\n"
                 "reminders 执行权威（三者拼不出状态机）\n"
                 "§3 #17｜§2.2 L7｜docs/decisions/架构评估.md:165-172｜D1 §4.29", "现状缺失"),
        ]),
        # ── L8 出站边界（旧泳道「出站层」，§1.3 跳 27–33） ────────────────
        Lane("L8", "出站边界", C_P0,
             "§1.3 跳 27–33｜唯一入口已收口；禁语/字数/断言恒 observe 已落地，旁路未登记", [
            Node("gw", C_MATCH, "OutboundGateway（跳 27）",
                 "权限 → 策略 → 闸门 → 审计｜会话回复/主动触达/提醒投递/Web/QQ 全走它\n"
                 "绿：core/speech/outbound.py:95（唯一出站边界）", "已达标"),
            Node("guards", C_FIXED, "闸门只观测已落地（P0-WP2 交付）",
                 "跳 27′｜禁语①/群聊字数②/无证据断言④ 恒 observe：只写 findings/审计/计数，\n"
                 "文本逐字不动（连 `on` 也一样）；整套改写机制已删（删子句 / 句边界截断 / 重生成）\n"
                 "重复指纹③ + 沉默熔断保留强制：③ 命中降级为短确认，block_repeats 且无短确认才 DROP；\n"
                 "熔断 = 同一会话 10 分钟内 DROP ≥3 → 一律 LOG_ONLY\n"
                 "观测项/拦截项拆两个 flag：OUTPUT_GATE_OBSERVE（写死 observe）/ OUTPUT_GATE_ENFORCE\n"
                 "§7 #1/#14 已落地｜P0 G4-1｜R-O1/R-O2/R-O3", "√已修"),
            Node("provider", C_PART, "provider → 平台执行",
                 "跳 29–31｜wx_sender / wx_sticker_sender / qq_provider\n"
                 "一个平台两条架构：微信表情包走 IntentBus、文字走 Provider（G5-7）", "部分"),
            Node("prechk", C_P0, "policy_prechecked 只读不写",
                 "executor 检查它，全仓无任何赋值点 → SEND_TEXT/SEND_FILE 走进永久失败态\n"
                 "「发送唯一入口」靠一句永真的字符串检查｜P1 G5-5｜D2 §4-1", "P1"),
            Node("fake3", C_P0, "3 处「以为发了其实没发」",
                 "① send_file 无落笔检查无回读、直接 ok=True（可能真发出路径文本）\n"
                 "② 表情包「面板关了就算成功」（_region_changed 算了不参与判定）\n"
                 "③ 微信把本机时间戳当 msg_id（confirmed_msg_id 永为 NULL）\n"
                 "P1 G5-2/G5-3、P2 G5-4｜D2 §4-8/4-11/4-17", "P1"),
            Node("bypass7", C_P0, "7 条绕过网关直调",
                 "旁路清单 B1–B7（含休眠与文档漏登）：wx_api.send_sticker（休眠却被 __all__ 宣称稳定）\n"
                 "· notify 全通道广播（scheduler 8 处直调）· wx_executor 自建 Provider 直调 provider.send\n"
                 "· wx_diagnose cmd_send 真发 · WxSender.get_instance · probe 鼠标\n"
                 "P1 G5-14｜D2 §6.1②｜旁路需接入网关或显式登记", "P1"),
        ]),
        # ── L9 平台执行（目标保留且明确独立；§2.2 L9）────────────────────
        Lane("L9", "平台执行", C_PART,
             "§2.2 L9｜明确独立（不同失效域）；现状：微信 UI 自动化 + QQ 协议 + Web", [
            Node("wx", C_MATCH, "微信 UI 自动化（保留）",
                 "wx_vision / wx_chat / wx_sticker_sender / wx_sender / wx_human\n"
                 "§2.2 L9（已存在）｜docs/架构所有权地图.md §三 #2：不同失效域，明确独立\n"
                 "要补：发送只能单进程守卫 + 失败分类", "已达标"),
            Node("qq2", C_MATCH, "QQ OneBot 协议（可作样板）",
                 "qq_provider 710 行｜§2.2 L9｜QQ 侧真闭环（对账 D2:100）", "已达标"),
            Node("web2", C_FIXED, "Web 管理面（安全三门槛已修）",
                 "§3 #34 → 收编：任意路径读文件已修——媒体根白名单 + 扩展名白名单单点判定\n"
                 "（safe_media_path / safe_media_name），越界一律 404，覆盖 /media/image 与 /api/local-file\n"
                 "管理面伪造 owner 已修：非本机 → 403 + 审计（structlog + append-only JSONL：\n"
                 "operator / operator_host / impersonated_owner / allowed / reason），覆盖 /api/memory/* 与 /api/recall\n"
                 "守卫 tests/test_web_security.py（6 用例）｜P0 G5-10/11/12｜F §4 F22/F25/F27", "√已修"),
        ]),
        # ── L10 记忆层（旧泳道「记忆与人物回流」；§2.2 记忆层 + §4 G3-2） ──
        Lane("L10", "记忆层", C_P0,
             "§1.2 跳 15／§4 G3-2｜无 claim 台账；可见性已下沉数据层（P0），纠正仍缺", [
            Node("facts7", C_P0, "7 处把用户话存成事实",
                 "全仓没有 claim 台账：投影 3 处（fact / relationship / self，她自己的话也成事实）\n"
                 "+ 显式 remember 工具 + Web 服务（无 owner 门）+ 纠错学习 + 名单同步\n"
                 "P1 G3-3｜E §重点③ 7 行表", "P1"),
            Node("vis", C_FIXED, "可见性已下沉数据层（P0-WP5 交付）",
                 "list_for_person 已加行级 scope 强制 + fail-closed：非 owner 走行级 SQL\n"
                 "（租户 + sensitivity 归一 + visibility 排除 owner_only/sensitive + 群/私聊按 occurred_scope），\n"
                 "代码里没有任何「无过滤」分支；缺/非法 scope → [] + person_memory_scope_denied\n"
                 "聚合读另拆显式非召回 API list_for_projection（跨范围聚合可以，跨范围召回不行）\n"
                 "守卫 tests/test_memory_scope.py（8 用例，含「越界必须被拒」）\n"
                 "§7 #11/#12 已落地｜P0 G3-2｜R-I1/I3 硬验收项｜E §重点②", "√已修"),
            Node("dead3b", C_P0, "3 表 0 行 + 话题 owner 不存在",
                 "topics / threads / thread_msg_ref 创建路径不可达、实测 0 行\n"
                 "core/context/threads.py 实测不存在；话题簇 3 组近似实现互不共享边界\n"
                 "P0 G2-2 / §3 #12｜B §4.6-5", "P0"),
            Node("nocorr", C_P0, "四层无在线纠正",
                 "只有投影层有纠正回路；person_notes / person_observations / person_profiles\n"
                 "/ memory_proposals 四者都没有被纠正的入口（「她记错了」只有一条闭环）\nP1｜E §重点①", "P1"),
        ]),
    ]


def fig1_geom(width: float):
    content_w = width - M * 2 - LABEL_W - LBL_GAP - FB_W
    return content_w


def _is_missing(nd: Node) -> bool:
    """「现状缺失」结构框：灰色 + 虚线 + 小一号字（图 1 新增结构行专用）。"""
    return bool(nd.dashed and nd.color == C_DEAD)


def _draw_merge_note(d: ImageDraw.ImageDraw, chip: tuple[str, str, str],
                     x0: float, x1: float, cy: float) -> None:
    """右侧「去向」列里的旧阶段名去向注记（图 1 专用）。

    chip = (旧阶段名, 去向说明, 颜色)。画法：横向彩色箭头 + 「旧名 / → 去向」两行文字。
    颜色与主干箭头（深灰）、回流箭头（深灰 / 红虚线）都不同 → 不会看混。
    """
    old, dest, color = chip
    d.text((x0, cy - 13), old, font=font(18, bold=True), fill=color, anchor="lm")
    d.text((x0, cy + 10), "→ " + dest, font=font(14), fill=INK_SOFT, anchor="lm")
    poly_arrow(d, [(x0 - 8, cy - 2), (x1, cy - 2)], color, 3, head=12)


def draw_fig1_body(c: Canvas, width: float, titles_only: bool = False) -> dict:
    """把图 1 画进画布；返回布局信息（行跨度 spans、结束 y、几何）。

    行名 = 目标架构层名（§2.2 表），与图 2 同一套；旧阶段名的去向写在右侧去向列。
    """
    d = c.d
    y = title_block(
        c, "现状架构全景（层名与目标架构同一套：L0–L10）",
        "一条消息 38 跳（主链 33 + 带 ′ 分支 5）｜判定 23 行 / 管道 15 行｜"
        "行名＝目标层名；灰色虚线行＝目标有、现状缺的结构",
        [("乱点 76 条（181 去重）", C_P0), (P0_FIXED_BADGE, C_FIXED),
         ("P0 21", C_P0), ("P1 44", C_PART), ("P2 11", C_DEAD), ("现状缺失 5 层", C_DEAD)],
        width,
    )
    y += legend(d, M, y + 14, LEGEND_LABELS + TO_LEGEND_FIG1, width - 2 * M,
                arrow_note=ARROW_LEGEND) + 18

    lanes = lanes_fig1()
    content_w = fig1_geom(width)
    bar_x0, bar_x1 = M + LABEL_W + LBL_GAP, width - M - FB_W - 18
    fb_left = width - M - FB_W
    x_arrow_tip = fb_left - 16   # 去向箭头尖端（贴住行框右边界）
    spans: dict[str, tuple[float, float]] = {}
    merge_pts: list[tuple[float, float]] = []

    for li, lane in enumerate(lanes):
        groups = lane_rows(len(lane.nodes))
        laid: list[list[tuple[Node, list[str], list[str], float]]] = []
        lane_h = 0.0
        for gi, cnt in enumerate(groups):
            bw = (content_w - (cnt - 1) * BOX_GAP) / cnt
            row_items = []
            row_h = 104.0
            for nd in lane.nodes[sum(groups[:gi]):sum(groups[:gi]) + cnt]:
                # 「现状缺失」行按小一号字排，行高更紧（只是交代一句现状）
                miss = _is_missing(nd)
                fs_t, fs_b = (20, 15) if miss else (22, 17)
                lh_t, lh_b = (26, 21) if miss else (29, 23)
                tl = wrap(d, nd.title, font(fs_t, bold=True), bw - 32)
                bl = wrap(d, nd.body, font(fs_b), bw - 32)
                row_items.append((nd, tl, bl, bw))
                row_h = max(row_h, 30 + len(tl) * lh_t + len(bl) * lh_b + 24)
            laid.append(row_items)
            lane_h += row_h + (ROW_GAP if gi else 0)
        lane_h += 30

        y0, y1 = y, y + lane_h
        spans[lane.num] = (y0, y1)
        # 行底色：灰虚线行 = 目标有 / 现状缺失；其余红行 = P0 乱点
        if lane.dashed or lane.color == C_DEAD:
            bg, bg_outline = FILLS[C_DEAD], C_DEAD
        elif lane.color == C_P0:
            bg, bg_outline = "#FFFBFB", LINE
        else:
            bg, bg_outline = "#FAFBFD", LINE
        d.rounded_rectangle([M, y0, bar_x1, y1], radius=14, fill=bg)
        if lane.dashed:
            dashed_rrect(d, [M, y0, bar_x1, y1], 14, bg_outline, 2)
        else:
            d.rounded_rectangle([M, y0, bar_x1, y1], radius=14, outline=bg_outline, width=2)
        lab_x0, lab_x1 = M + 14, M + 14 + LABEL_W - 28
        if lane.dashed:
            d.rounded_rectangle([lab_x0, y0 + 12, lab_x1, y1 - 12], radius=12, fill=PAGE)
            dashed_rrect(d, [lab_x0, y0 + 12, lab_x1, y1 - 12], 12, lane.color, 3)
            lab_fill = lane.color
        else:
            d.rounded_rectangle([lab_x0, y0 + 12, lab_x1, y1 - 12], radius=12, fill=lane.color)
            lab_fill = "#FFFFFF"
        _lane_caption(d, lane, lab_x0, lab_x1, (y0 + y1) / 2, lab_fill)

        ry = y0 + 15
        for row_items in laid:
            fs = (20, 15, 26, 21) if _is_missing(row_items[0][0]) else (22, 17, 29, 23)
            hh = max(104.0, max(30 + len(t[1]) * fs[2] + len(t[2]) * fs[3] + 24
                                for t in row_items))
            bx = bar_x0 + 10
            for nd, tl, bl, bw in row_items:
                x0, x1 = bx, bx + bw
                fill = FILLS.get(nd.color, PAGE)
                if nd.dashed:
                    d.rounded_rectangle([x0, ry, x1, ry + hh], radius=12, fill=fill)
                    dashed_rrect(d, [x0, ry, x1, ry + hh], 12, nd.color, 3)
                else:
                    d.rounded_rectangle([x0, ry, x1, ry + hh], radius=12, fill=fill,
                                        outline=nd.color, width=3)
                miss = _is_missing(nd)
                fs_t, fs_b = (20, 15) if miss else (22, 17)
                lh_t, lh_b = (26, 21) if miss else (29, 23)
                tx, ty = x0 + 16, ry + 13
                if nd.badge_text:
                    bwid = text_w(d, nd.badge_text, font(16, bold=True)) + 20
                    d.rounded_rectangle([tx, ty - 2, tx + bwid, ty + 24], radius=7, fill=nd.color)
                    d.text((tx + bwid / 2, ty + 11), nd.badge_text, font=font(16, bold=True),
                           fill="#FFFFFF", anchor="mm")
                    ty += 30
                for ln in tl:
                    d.text((tx, ty), ln, font=font(fs_t, bold=True),
                           fill=INK_SOFT if miss else INK, anchor="la")
                    ty += lh_t
                ty += 4
                for ln in bl:
                    d.text((tx, ty), ln, font=font(fs_b),
                           fill=INK_MUTED if miss else INK_SOFT, anchor="la")
                    ty += lh_b
                bx = x1 + BOX_GAP
            ry += hh + ROW_GAP

        # 右侧「去向」列：旧阶段名 → 并入哪一层（彩色箭头，与主干箭头区分）
        if lane.merge:
            _draw_merge_note(d, lane.merge, x_arrow_tip + 16, x_arrow_tip + 8, (y0 + y1) / 2)
            merge_pts.append((x_arrow_tip + 8, (y0 + y1) / 2 - 2))

        if li != len(lanes) - 1:
            arrow_down(d, bar_x0 + content_w / 2, y1 + 3, y1 + 40)
        y = y1 + 44

    return {"spans": spans, "y_end": y, "content_w": content_w, "fb_left": fb_left,
            "merge_pts": merge_pts}


def draw_fig1_feedback(c: Canvas, width: float, geom: dict, draw_it: bool = True) -> None:
    """图 1 右侧列 = 上半「回流 / 回流缺口」+ 下半「旧阶段名去向」：

      回流三条（层级名已对齐目标架构）：
        ① L8 出站边界 → L10 记忆层：出站结果回流，写 send_log / outbound_audit（§1.3 跳 32–33）
        ② L10 记忆层 → L0 事实底座：记忆行必须可回溯源事件（投影 3 写点）
        ③ L2 意图层/判定 → L0 事实底座：现状不存在这条「在线纠正」（红虚线＝缺口，P1 G3-3 / E §重点①）

      去向三条（只出现在图 1；颜色与主干箭头、回流箭头都不同）：
        摄取与预处理 → 并入 L0　触发 → 并入 L2／决策　工具层 → 并入 L3 内层
        旧阶段名的完整映射表见主稿 §0.4「两套分层映射表」。
    """
    d = c.d
    spans = geom["spans"]
    x_fb = geom["fb_left"]
    y_top = spans["入站"][0]
    y_bot = geom["y_end"] - 44
    d.rounded_rectangle([x_fb, y_top - 14, width - M, y_bot], radius=16,
                        fill="#F6F8FB", outline=LINE, width=2)
    d.text(((x_fb + width - M) / 2, y_top + 18), "回流 / 回流缺口 + 旧阶段名去向",
           font=font(21, bold=True), fill=INK, anchor="mm")
    d.text((x_fb + 16, y_top + 48), "① L8 出站 → L10 记忆　② L10 记忆 → L0 事实",
           font=font(15), fill=INK_MUTED, anchor="lm")
    d.text((x_fb + 16, y_top + 70), "③ L2 判定 → L0：在线纠正（现状缺口，红虚线）",
           font=font(15), fill=C_P0, anchor="lm")
    if not draw_it:
        return

    right_lane = spans["L8"]    # 出站边界（回流①起点）
    mem_lane = spans["L10"]     # 记忆层（回流①终点 / ②起点）
    judge_lane = spans["L2"]    # 意图层（回流③缺口起点）
    fact_lane = spans["L0"]     # 事实底座（回流②③终点）

    x_lab = x_fb + 16          # 文字左边界
    x_tip = x_fb - 10          # 箭头尖端（指向泳道）
    x_mid = x_fb + 128         # 竖向转折线

    # 三条回路的标签行必须互不重叠：按行高 100 依次下排，再统一做一次防撞调整
    r1 = right_lane[1] + 62
    r2 = r1 + 100
    r3 = r2 + 100
    min_r2, min_r3 = mem_lane[0] - 40, judge_lane[1] + 20
    while r2 < min_r2 or r3 < min_r3:
        r1 += 12
        r2 += 12
        r3 += 12
    base = fact_lane[1] + 8

    # ① L8 出站 → L10 记忆/投影
    poly_arrow(d, [(x_tip, r1), (x_fb + 168, r1), (x_fb + 168, mem_lane[0] - 6)], C_ARROW, 3)
    d.text((x_lab, r1 - 32), "① L8 出站 → L10 记忆/投影", font=font(16, bold=True), fill=INK,
           anchor="lm")
    d.text((x_lab, r1 - 11), "写 send_log / outbound_audit", font=font(14),
           fill=INK_SOFT, anchor="lm")
    d.text((x_lab, r1 + 9), "（§1.3 跳 32–33）", font=font(14), fill=INK_MUTED, anchor="lm")

    # ② L10 记忆/投影 → L0 事实底座
    poly_arrow(d, [(x_tip, r2), (x_mid, r2), (x_mid, base)], C_ARROW, 3)
    d.text((x_lab, r2 - 32), "② L10 记忆 → L0 事实底座", font=font(16, bold=True), fill=INK,
           anchor="lm")
    d.text((x_lab, r2 - 11), "记忆行必须可回溯源事件", font=font(14), fill=INK_SOFT, anchor="lm")
    d.text((x_lab, r2 + 9), "投影 3 写点 → memory_projections", font=font(14),
           fill=INK_MUTED, anchor="lm")

    # ③ 缺口：L2 判定 → L0 事实底座（红色虚线）
    poly_arrow(d, [(x_tip, r3), (x_mid - 66, r3), (x_mid - 66, base)], C_P0, 3, dashed=True)
    d.text((x_lab, r3 - 32), "③ L2 判定 → L0：在线纠正", font=font(16, bold=True),
           fill=C_P0, anchor="lm")
    d.text((x_lab, r3 - 11), "现状不存在这条回路（红色虚线＝缺口）", font=font(14),
           fill=C_P0, anchor="lm")
    d.text((x_lab, r3 + 9), "P1 G3-3｜E §重点①", font=font(14), fill=C_P0, anchor="lm")

    # 去向列：用一条淡淡虚线竖轴把三个彩色箭头串起来，末尾再放一份小图例
    pts = geom.get("merge_pts") or []
    if pts:
        _dashed_line(d, [(x_fb - 14, pts[0][1]), (x_fb - 14, pts[-1][1])], C_DEAD, 2,
                     dash=6, gap=8)
        y_lg = pts[-1][1] + 34
        d.text((x_fb + 16, y_lg), "旧阶段名去向（主稿 §0.4 映射表）：", font=font(15, bold=True),
               fill=INK, anchor="lm")
        for i, (txt, col) in enumerate([("摄取与预处理 → 并入 L0", C_TO_L0),
                                        ("触发 → 并入 L2／决策", C_TO_L2),
                                        ("工具层 → 并入 L3 内层", C_TO_L3)]):
            cy = y_lg + 28 + i * 26
            d.ellipse([x_fb + 16, cy - 6, x_fb + 28, cy + 6], fill=col)
            d.text((x_fb + 36, cy), txt, font=font(14), fill=col, anchor="lm")


def draw_fig1() -> tuple[Path, tuple[int, int, int]]:
    probe = Canvas(W1, PROBE_H)
    geom = draw_fig1_body(probe, W1)
    total = geom["y_end"] + 34 + 4 * 25 + 40   # 页脚 4 行
    c = Canvas(W1, total)
    geom = draw_fig1_body(c, W1)
    draw_fig1_feedback(c, W1, geom, draw_it=True)
    footer(c, total - 143, W1,
           "口径：本图是 P0 交付后的状态（刷新日期 2026-10-01）。"
           "层名（与目标架构同一套）：本图行名直接用 §2.2 表的 L0–L10 —— "
           "入站（L9 的对侧）/ L0 事实底座 / L1 归属与指代 / L2 N1 意图层 / L3 N2 规划-执行 / L4 N3 焦点状态 / "
           "L5 N4 规则引擎 / L6 生成与人格 / L7 N5 参数状态机 / L8 出站边界 / L9 平台执行 / L10 记忆层。"
           "配色：绿＝已达标 / 青绿「√已修（P0）」＝本框记的乱点已由 P0 交付消掉 / 黄＝部分需收编 / 红＝仍是乱点 / 灰＝退役或现状缺失 / 蓝＝新增。"
           "灰色虚线行 = 目标有、现状缺的结构（L2/L3/L4/L5/L7 这 5 行的「现状缺失」框）；"
           "旧阶段名的去向（青/紫/橙箭头）见右侧列，完整映射见 §0.4「两套分层映射表」。"
           "本次刷新改了 6 处：①CoarseAssigner 已删（不再是现状框）②L2 补「契约已建成、未接线」"
           "③L4 改「状态载体已建、判定未通电」④L8 闸门改「禁语/字数/断言恒 observe」"
           "⑤L10 可见性改「已下沉数据层」+ 入站/L9 三处安全门槛改「√已修」⑥L0 第二 INSERT 写方改「已收编」。"
           "读图要点（全部引自 docs/全链路架构审计与目标架构.md）：横向底盘（事实/身份/时序/归属/绑定/出站网关）"
           "大部分已收口（§3 匹配 4 行）；不匹配的 16 行全部是「纵向能力层 + 边界面」（§3 读法）。"
           "§1.3 核心读法：管道行绝大多数是对的，判定行几乎全是「在错的层、由错的 owner、用关键词做判断」"
           "——这正是 §5 根因 1–3 的来源。")
    p = OUT_DIR / "现状架构全景.png"
    return p, c.save(p)


# ===========================================================================
# 图 2 —— 目标架构全景（10 层）
# ===========================================================================
def layers_fig2() -> list[Lane]:
    """出处：主稿 §2.1 分层图（L0–L9）+ §2.2 契约表 + §2.3 收编/退役清单 + §6/§7。"""
    return [
        Lane("L0", "事实底座", C_MATCH, "§2.2 L0｜保留（已收口）｜只需收编第二写方", [
            Node("owner1", C_MATCH, "唯一 owner INSERT（已收口）",
                 "terminal_events：append-only · 库内原子取号 · 四元组幂等\n"
                 "§2.2 L0｜A §5.1「17 条已做对」\n纪律：只产事实与证据，不下「该理解成什么」", "已达标"),
            Node("claim2", C_NEW, "claim 台账 · 新增",
                 "候选 → 确认；现状没有 claim 台账 = 7 条路径把用户话原样存成事实\n"
                 "P1 G3-3｜E §重点③｜§7 #16：给投影 fact 打 needs_review", "新增"),
            Node("ev2fix", C_FIXED, "第二 INSERT 写方 → 已收编（P0-WP4）",
                 "已落地：owner 增 append_in_session(session, event)，core/tasks/store.py 改调它；\n"
                 "三道保护（幂等提前返回 / 四元组回查 / RETURNING sequence 非空校验）是 owner 里的\n"
                 "同一份实现；白名单收缩到 1 个文件 + 重放场景用例（tests/test_event_chain_links.py）\n"
                 "P0 G2-1｜§7 #7｜§2.2 L0 收编目标｜A:628 选项①", "√已修"),
            Node("kwfix", C_PART, "关键词意图 → 降级为证据",
                 "P0 G2-3｜event_type 统一；结论只落 metadata.intent_hint\n"
                 "§2.3：先让关键词结论降级为证据（N1 契约的前置）", "P0"),
        ]),
        Lane("L1", "归属与指代", C_MATCH, "§2.2 L1｜保留，唯一 owner 且守卫绿", [
            Node("attr3", C_MATCH, "归属边（已达标）",
                 "addressing.py:623 resolve_attribution｜R-B1/R-T3 已满足\n"
                 "§2.2 L1｜要把 5 份 pat 正则、2 份引用头、7 份问句判定收编回来", "已达标"),
            Node("ref3", C_MATCH, "指代绑定（已达标）",
                 "core/context/referent.py:220 bind_pronouns｜一处判一处用没有漂\n"
                 "§2.2 L1｜R-B1/R-T3 已满足，守卫绿", "已达标"),
        ]),
        Lane("L2", "N1 意图层", C_NEW, "§2.2 L2【新增】｜「怎么理解」的唯一决策点", [
            Node("n1", C_NEW, "大 LLM 唯一 owner · 新增",
                 "owner：core/contracts/reply_intent.py + core/context/intent.py（新）\n"
                 "输出 IntentResult{intent, attitude, confidence, evidence[], candidates[],\n"
                 "ambiguity_flags, need_clarify, clarify_candidates[], register, degraded}\n"
                 "§2.2 L2｜纪律：只决定「怎么理解」，不决定「说什么」（后者是 L3）", "新增"),
            Node("att9", C_NEW, "9 种态度（替 6 值姿态）",
                 "调侃/认真/赞同/反对/戏谑/关心/拒绝/卖萌/卖惨（R-A3）\n"
                 "P0 G1-2｜§2.3：stance.decide_stance 6 值降级为证据（位置/气氛事实），态度取值由 LLM 出", "P0"),
            Node("evi", C_PART, "92 判定点 → 只产证据",
                 "P0 G1-1｜硬闸门 / 视野裁剪 / 结论注入 / 出口改写四形态一律降级为证据，喂 N1\n"
                 "§2.2 L2 必须收编：admission 逐行相关性降为窗口过滤、fast_path 7 组词降为前置过滤器、\n"
                 "硬编码话术已删（WP-01d）、signals.topic_similarity 退役", "P0"),
            Node("clarify", C_NEW, "不懂就反问（need_clarify）",
                 "§7 #3：落理解层（N1 产 need_clarify / clarify_candidates）；保留「同一话题最多一次」\n"
                 "+ 每会话 10 分钟上限；「该静默」与「该反问」冲突时反问优先（R-A8/R-D1 硬条款）", "新增"),
            Node("admit", C_MATCH, "准入合并进同一次调用",
                 "§6 P1 净增量结论：若 N1「替代」admission_judge 且「补触发路径」复用同一次调用\n"
                 "→ P0+P1 稳态新增调用数可做到 0（docs/decisions/架构评估.md:423）\n"
                 "若选「并列」则群聊 ambient 回合 2 → 3 次", "0 调用"),
            Node("contract", C_FIXED, "ReplyIntent 契约 · 已建成（P0-WP1）",
                 "契约已建成：core/contracts/reply_intent.py（551 行）——intent 6 值 + attitude 9 值封闭集合、\n"
                 "置信度三条腿、need_clarify 三条件真值表、threshold/weight 都是参数（R-A6 待评测集校准）\n"
                 "尚未接线：flag reply_intent 默认 shadow，全仓无读者；新增 LLM 调用 = 0\n"
                 "接线时 router.py:930 与 :1272 两处都要调（R-A1 判「改骨架」的落点）\n"
                 "§2.2 L2｜不碰 conversational.py 内部｜§7 #2：注入极简一行（如「态度: 调侃」）", "√已修"),
            Node("trigger", C_P0, "触发路径也过准入",
                 "缺口 G7 守卫不存在（docs/架构所有权地图.md:191）\n"
                 "现状 router.py:1424-1427 注释逐字「不花这次 judge」→ 目标：触发路径补上准入", "P0"),
            Node("defaults", C_PART, "闸门默认态反转",
                 "P1 G4-3｜默认开的全是关键词闸门、默认关的全是结构减法与社交事实（21 个 flag 速查）\n"
                 "§6 P1：建议随 R-A3 新层落地同步反转", "P1"),
        ]),
        Lane("L3", "N2 规划-执行", C_NEW, "§2.2 L3【新增】：包裹不是替换｜外层 + 内层", [
            Node("outer", C_NEW, "外层步骤：每步三字段 · 新增",
                 "Step{needed_info, expected_output, success_criteria, …} 有序序列\n"
                 "owner：agents/plan_loop.py（新）+ core/contracts/content_plan.py（新）\n"
                 "+ planner.py:55 SubTask 扩字段｜§2.2 L3｜R-C1..C7", "新增"),
            Node("inner", C_PART, "内层对比：verify_step 进日常回合",
                 "现状 _verify_step 只判 2 条（空召回 / 搜索范围不符）且原生循环一次都没调\n"
                 "§2.2 L3：唯一必须碰主循环处 = observation 生成后加一次 verify_step（约 3–5 行）", "收编"),
            Node("switch3", C_NEW, "换方法三级（尝试 ≤3 次）",
                 "§7 #5：按实现定措辞「尝试 ≤ 3 次（= 最多换 2 次方法）」，同步修订契约 R-C5 并重跑基线\n"
                 "验收：槽位补齐率 / 单步换方法成功率 / 超预算上报率", "新增"),
            Node("retire_ts", C_DEAD, "task_session 69 行 → 退役",
                 "§2.3：agents/task_session.py 退役 → L3 N2 + work_items 台账\n"
                 "触发条件：内存字典零残留 + work_items 行数 = 任务数", "退役", dashed=True),
        ]),
        Lane("L4", "N3 焦点状态", C_NEW, "§2.2 L4【新增】：session_focus 每会话一行", [
            Node("focus3", C_FIXED, "焦点状态 · 状态载体已建（P0-WP3）",
                 "已落地：db/repo_focus.py + 迁移 54（db/migrations.py:2051）建 session_focus，\n"
                 "每会话一行、主键 (platform, account_id, chat_id)、12 列与 meta_models.SessionFocusState 同形\n"
                 "但只落状态载体、0 行：聚类内核 core/context/threads.py（实测不存在）\n"
                 "与消费方注入属 P1（本轮不接线）\n"
                 "FocusResult{anchor, anchor_confidence, stack, primary_thread, …}\n"
                 "§2.2 L4｜§7 #4：新建单表 + parity 守卫｜R-T1/T5、R-G1/G2、R-B3/B4", "√已修"),
            Node("cluster3", C_PART, "room_state 聚类 → 升格为内核",
                 "§2.3：升格为 N3 的聚类内核；门槛 = score_threads_eval 五项全过 + shadow 真流量 ≥48h\n"
                 "topic_line 改「取 owner 事实再出话」；signals.topic_similarity 死字段退役", "收编"),
        ]),
        Lane("L5", "N4 规则引擎", C_NEW, "§2.2 L5【新增，复用既有数据面】｜Web UI 可配", [
            Node("rules", C_NEW, "五维规则 · 新增",
                 "owner：core/rules/engine.py（新）+ 读通道 core/speech/store.py:110（已存在）\n"
                 "五维 condition / target / quota / action / priority（R-V2）\n"
                 "RuleDecision{allowed, specificity, explanation, shadow}｜§2.2 L5｜§7 #1 闸门拆两 flag", "新增"),
            Node("hot", C_MATCH, "改数据不重启即生效",
                 "复用同一张表同一通道（policy_rules + 按 get_revision() 缓存）\n"
                 "→ 改一行数据即生效、不重启｜绿：§2.2 L5｜R-V4｜§6 P1「是否重启」", "已达标"),
            Node("hardblock", C_NEW, "硬禁区写死代码不落数据行",
                 "金钱 · 隐私 · 代发他人 · 记忆范围 R-I1\n（连存在都不许在 UI 出现，R-I2）｜§2.2 L5", "新增"),
            Node("merge4", C_PART, "收编：三本账 / 5 处工具内判定",
                 "§2.3：主动开口阈值三本账→一本、提醒配额 NON_OWNER_PENDING_CAP=5 常数上移到数据行、\n"
                 "出站闸门阈值保持数据驱动、动作前置条件 12 处收编｜P0 G1-13", "收编"),
        ]),
        Lane("L6", "生成与人格", C_PART,
             "§2.2 L6｜保留骨架（方案 A = 新层包裹，不拆 conversational.py）", [
            Node("convimpl", C_PART, "conversational 收编为「语言实现」",
                 "§2.2 L6：conversational.py 一行不改（除必要的 flag 读取点）；方案 A 包裹，不拆\n"
                 "P1 G1-8：抽单点（_synthesize_reply 改为转发 stream 版）", "收编"),
            Node("persona", C_MATCH, "persona / assembler 保留",
                 "persona_brain.build_persona_blocks 仍是块的唯一生产 owner（20 块）\n"
                 "assembler._BLOCK_POLICY 是块清单唯一登记处（新块必须登记，否则 undeclared）\n"
                 "绿：§2.2 L6｜人格/语气明确独立（变更频率最高、要可回滚）", "已达标"),
            Node("gate_obs", C_FIXED, "闸门只观测 · 已落地（P0-WP2）",
                 "已落地：禁语①/群聊字数②/无证据断言④ 恒 observe（只写 findings/审计/计数，\n"
                 "文本逐字不动，连 `on` 也一样）；重复指纹③ + 沉默熔断保留强制；整套改写机制已删\n"
                 "§7 #1/#14 已落地：观测项与拦截项分流成两个 flag（OUTPUT_GATE_OBSERVE 写死 observe /\n"
                 "OUTPUT_GATE_ENFORCE 默认 enforce），不再共用一个 OUTPUT_GATE\n"
                 "P0 G4-1｜R-O1/R-O2/R-O3｜守卫 tests/test_output_gate.py", "√已修"),
            Node("freeze", C_NEW, "context.extra 冻结契约",
                 "N1/N2/N3/N4 的结论一律经 context.extra 注入并冻结契约（≤20 键）\n"
                 "且必须有「意图层说要说、主循环不许沉默」的守卫｜§2.2 L6", "新增"),
        ]),
        Lane("L7", "N5 参数状态机", C_NEW, "§2.2 L7【新增，复用台账面】｜收集→确认", [
            Node("slot", C_NEW, "收集→校验→引用+确认→执行 · 新增",
                 "状态 GATHERING→VALIDATING→CONFIRMING→EXECUTING→DONE/ABORTED\n"
                 "owner：core/tasks/slot_fill.py（新）+ 台账 core/tasks/store.py；权威侧是 reminders\n"
                 "硬前置：kind 级 schema 校验（docs/archive/任务清单_2026-10-06.md:50 B5）｜§2.2 L7｜R-P1..P8", "新增"),
            Node("confirm", C_PART, "确认协议：引用 + 确认（替「可以继续吗」）",
                 "现状只有一次「我准备这样做：…可以继续吗？」（不记 pending 状态、无引用校验）\n"
                 "§2.3：确认协议要从「可以继续吗」改成「引用 + 确认」；mapping.py 作校验器", "收编"),
            Node("ledger", C_PART, "work_items 补读取面 / 取消改期",
                 "§3 #29 部分｜缺①谁下达链路②取消/改期入口③读取面（Web 无 work_items 路由）④迁移未写\n"
                 "P1 G5-16/G5-17｜§7 #8：todos 收敛成 work_items 一个 kind", "部分"),
            Node("rm", C_MATCH, "reminders 执行权威（保留）",
                 "§2.2 L7：权威侧是 reminders（agents/tools/reminder.py:242），\n"
                 "work_items 只是单向读模型，绝不反向", "已达标"),
        ]),
        Lane("L8", "出站边界", C_PART,
             "§2.2 L8 保留｜唯一收口 OutboundGateway", [
            Node("gw2", C_MATCH, "OutboundGateway 保留",
                 "core/speech/outbound.py:95；会话回复/主动触达/提醒投递/Web/QQ 全走它\n§2.2 L8 保留（唯一收口）", "已达标"),
            Node("idem", C_PART, "幂等键 + 唯一索引",
                 "P1 G5-1｜§7 #20：先登记「什么算同一条」（平台+账号+chat+内容 hash+时间窗）；\n"
                 "补齐之前绝不在「先发后写」之间插入任何重试逻辑｜§7 #9：明确单进程 + 启动守卫", "P1"),
            Node("verify", C_PART, "回读验证（confirmed / unverified）",
                 "P1 G5-2/G5-3｜send_file 复用落笔检查 + 发送后回读；表情包把 chat_changed 纳入判定；\n"
                 "返回值区分 confirmed / unverified｜G5-4 msg_id 改名 local_sent_at", "P1"),
            Node("onepath", C_PART, "收编旁路：一条平台一条架构",
                 "P1 G5-14｜notify 接入网关或显式登记「未经闸门 + 理由」；wx_api.send_sticker 删除或必须传 gateway\n"
                 "P2 G5-7｜IntentBus vs Provider 先定一条；G5-6 wx_executor 自建 Provider 收编", "收编"),
        ]),
        Lane("L9", "平台执行", C_MATCH, "§2.2 L9｜保留且明确独立（不同失效域）", [
            Node("wx", C_MATCH, "微信 UI 自动化（保留）",
                 "wx_vision / wx_chat / wx_sticker_sender / wx_sender / wx_human\n"
                 "§2.2 L9（已存在）｜docs/架构所有权地图.md §三 #2：不同失效域，明确独立\n"
                 "要补：发送只能单进程守卫 + 失败分类", "已达标"),
            Node("qq2", C_MATCH, "QQ OneBot 协议（可作样板）",
                 "qq_provider 710 行｜§2.2 L9｜QQ 侧真闭环（对账 D2:100）", "已达标"),
            Node("web2", C_PART, "Web 管理面（收编）",
                 "§3 #34 离目标最远 → 收编：守卫扩到所有非 /static 路径；\n"
                 "引入显式「本地控制台主体」+ 审计行，不再冒充 owner｜P0 G5-10/11/12", "P0"),
            Node("ingress", C_PART, "入站采集（L9 的对侧；旧泳道「平台入站」）",
                 "§3 #1 部分｜跳 1–2：core/ingestion/ingestors.py:404；scheduler/main.py:173-187\n"
                 "第二条触发路径无 flag / 无审计 / 疑无调用方；__init__.py 用 import * 破坏显式边界\n"
                 "A §4.20/§4.21｜收编 = 登记为入口 + flag + 审计", "部分"),
            Node("wxwatch", C_P0, "微信 DB 文件监听（待定）",
                 "跳 1｜core/ingestion/db_watcher.py:16 脏标记…疑无生产构造点\n"
                 "全仓仅 core/ingestion/__init__.py:1 的 * 再导出（A:534）｜P0 G2-3 同域\n"
                 "§2.3 处置：无调用方 → 退役；在用 → 登记为采集入口 + flag + 审计（§7 #19）", "P0"),
        ]),
        Lane("L10", "记忆层", C_NEW, "§2.2 记忆层 + §4 G3-2｜数据层强制 + 在线纠正", [
            Node("visscope", C_FIXED, "可见性下沉数据层 · 已落地（P0-WP5）",
                 "已落地：db/repo_person_memory.list_for_person 加行级 scope 强制 + fail-closed\n"
                 "（非 owner 走行级 SQL：租户 + sensitivity 归一 + visibility 排除 owner_only/sensitive\n"
                 "+ 群/私聊按 occurred_scope）；代码里没有任何「无过滤」分支；缺/非法 scope → []\n"
                 "聚合读拆出显式非召回 API list_for_projection（可跨范围聚合、不可跨范围召回）\n"
                 "守卫 tests/test_memory_scope.py（8 用例，含「越界必须被拒」）\n"
                 "P0 G3-2｜R-I1/I3 硬验收项已满足｜§7 #11/#12", "√已修"),
            Node("corr", C_NEW, "在线纠正：四层补齐 · 新增",
                 "E §重点①｜纠错能力不对称：person_notes / person_observations / person_profiles /\n"
                 "memory_proposals 四者都没有被纠正的入口｜§7 #17：unresolved 终态 + 退避重试一次", "新增"),
            Node("deadtab", C_DEAD, "3 表 0 行 + 两套 Thread → 先定概念",
                 "§2.3 / §7 #4：先答「两套 Thread 是不是同一件事」；两套空表先不动\n"
                 "（docs/架构元规则.md:96「停用的表不删」）；CoarseAssigner 整类删（P0 G2-2）", "退役", dashed=True),
            Node("proj", C_PART, "投影写点收编（3 → 1）",
                 "P1 G2-8｜记忆投影有第二/第三个写方（remember 工具、纠错回写各建一份 projector）\n"
                 "→ owner 暴露 supersede 方法｜保证「每条记忆可回溯源事件」", "P1"),
        ]),
    ]


def draw_fig2_body(c: Canvas, width: float) -> float:
    d = c.d
    y = title_block(
        c, "目标架构全景（层名与现状图同一套：L0–L10）",
        "纵深补 5 个能力层（N1–N5）；横向骨架保留；平台执行明确独立（L9）",
        # 徽章数字全部可从 §2.3「收编清单」数出来：该表 25 行 = 保留 7 +
        # 收编/降级/改调 12 + 直接删或退役 4 + 先确认再定 2。
        # 「P0 已修 5 项」= §6 P0「目标」5 项 = 5 个交付批（口径见本文档头）。
        [("11 层（L0–L10）", C_NEW), (P0_FIXED_BADGE, C_FIXED), ("§2.3：保留 7", C_MATCH),
         ("收编/降级 12", C_PART), ("删或退役 4", C_DEAD), ("待确认 2", C_ARROW)],
        width,
    )
    y += legend(d, M, y + 14, LEGEND_LABELS, width - 2 * M,
                arrow_note="深灰实线箭头 ＝ 主干流向（自上而下）；灰色虚线框 ＝ 退役 / 待定") + 18

    lanes = layers_fig2()
    content_w = width - M * 2 - LABEL_W - LBL_GAP - 8
    for li, lane in enumerate(lanes):
        groups = lane_rows(len(lane.nodes))
        laid, lane_h = [], 0.0
        for gi, cnt in enumerate(groups):
            bw = (content_w - (cnt - 1) * BOX_GAP) / cnt
            row_items, row_h = [], 100.0
            for nd in lane.nodes[sum(groups[:gi]):sum(groups[:gi]) + cnt]:
                tl = wrap(d, nd.title, font(21, bold=True), bw - 30)
                bl = wrap(d, nd.body, font(16), bw - 30)
                row_items.append((nd, tl, bl, bw))
                row_h = max(row_h, 28 + len(tl) * 28 + len(bl) * 22 + 22)
            laid.append(row_items)
            lane_h += row_h + (ROW_GAP if gi else 0)
        lane_h += 26

        y0, y1 = y, y + lane_h
        d.rounded_rectangle([M, y0, width - M, y1], radius=14, fill="#FAFBFD", outline=LINE, width=2)
        lab_x0, lab_x1 = M + 14, M + 14 + LABEL_W - 28
        if lane.dashed:
            d.rounded_rectangle([lab_x0, y0 + 12, lab_x1, y1 - 12], radius=12, fill=PAGE)
            dashed_rrect(d, [lab_x0, y0 + 12, lab_x1, y1 - 12], 12, lane.color, 3)
            lab_fill = lane.color
        else:
            d.rounded_rectangle([lab_x0, y0 + 12, lab_x1, y1 - 12], radius=12, fill=lane.color)
            lab_fill = "#FFFFFF"
        _lane_caption(d, lane, lab_x0, lab_x1, (y0 + y1) / 2, lab_fill)

        ry = y0 + 13
        for row_items in laid:
            hh = max(100.0, max(28 + len(t[1]) * 28 + len(t[2]) * 22 + 22 for t in row_items))
            bx = M + LABEL_W + LBL_GAP + 3
            for nd, tl, bl, bw in row_items:
                x0, x1 = bx, bx + bw
                fill = FILLS.get(nd.color, PAGE)
                if nd.dashed:
                    d.rounded_rectangle([x0, ry, x1, ry + hh], radius=12, fill=fill)
                    dashed_rrect(d, [x0, ry, x1, ry + hh], 12, nd.color, 3)
                else:
                    d.rounded_rectangle([x0, ry, x1, ry + hh], radius=12, fill=fill,
                                        outline=nd.color, width=3)
                tx, ty = x0 + 15, ry + 12
                if nd.badge_text:
                    bwid = text_w(d, nd.badge_text, font(15, bold=True)) + 20
                    d.rounded_rectangle([tx, ty - 2, tx + bwid, ty + 24], radius=7, fill=nd.color)
                    d.text((tx + bwid / 2, ty + 11), nd.badge_text, font=font(15, bold=True),
                           fill="#FFFFFF", anchor="mm")
                    ty += 30
                for ln in tl:
                    d.text((tx, ty), ln, font=font(21, bold=True), fill=INK, anchor="la")
                    ty += 28
                ty += 3
                for ln in bl:
                    d.text((tx, ty), ln, font=font(16), fill=INK_SOFT, anchor="la")
                    ty += 22
                bx = x1 + BOX_GAP
            ry += hh + ROW_GAP
        if li != len(lanes) - 1:
            arrow_down(d, M + LABEL_W + LBL_GAP + content_w / 2, y1 + 3, y1 + 40)
        y = y1 + 44
    return y


def draw_fig2() -> tuple[Path, tuple[int, int, int]]:
    probe = Canvas(W2, PROBE_H)
    y_end = draw_fig2_body(probe, W2)
    total = y_end + 30 + 3 * 25 + 46
    c = Canvas(W2, total)
    draw_fig2_body(c, W2)
    footer(c, total - 118, W2,
           "口径：本图是 P0 交付后的状态（刷新日期 2026-10-01）。"
           "读图要点：两条共同纪律不许破（§2 引言）——① 每个新结构必须收编它覆盖的既有近似实现并让旧实现退役；"
           "② 新结构一律「先 shadow 后 on」，flag 走 core/features.py mode() 的三态读法"
           "（enabled() 会把 shadow 读成 False）。§2 基准：本文不新增第 6 个结构；七份审计也未提出需要推翻分层的新证据。"
           "本轮已落地的框标「√已修（P0）」（青绿）：L0 第二 INSERT 写方收编 · L2 ReplyIntent 契约已建成但未接线 · "
           "L4 session_focus 状态载体已建、判定未通电 · L6 闸门只观测 · L10 可见性下沉数据层；"
           "其余红/黄框照旧＝尚未落地，图上没有把未做的算成已做。")
    p = OUT_DIR / "目标架构全景.png"
    return p, c.save(p)


# ===========================================================================
# 图 3 —— 现状-目标对照（左右并列 + 中间箭头）
# ===========================================================================
@dataclass
class MapRow:
    status: str   # 匹配 / 部分 / 不匹配（§3 口径）
    left: str     # 左列现状（§3 能力层 + 现状 owner）
    action: str   # 中间箭头上的动作
    right: str    # 右列目标层
    cite: str     # 出处（§3 行号 / §4 编号）
    detail: str = ""


ACTION_COLORS = {
    "新增结构": C_NEW,
    "收编": C_PART,
    "退役": C_DEAD,
    "降级为证据": C_P0,
    "合并": "#8A5CD1",
}


def map_rows() -> list[MapRow]:
    """逐行照 §3 的 35 行「能力层」+ §2.3 收编/退役 + §6/§7 的动作整理。"""
    return [
        # ── 入站 / 事实层 ───────────────────────────────────────────────
        MapRow("部分", "#1 入站采集（core/ingestion；第二条触发路径无 flag 无审计）",
               "合并", "L9 平台执行 · 入站采集（L9 的对侧）", "§3 #1｜A §4.20/4.21",
               "Web 管理面一并收编（P0 G5-10/11/12）"),
        MapRow("不匹配", "#2 事实层写入（event_store_db:189；第二写方已收编）",
               "收编", "L0 事实底座 · owner 唯一 INSERT（P0-WP4 已落地）", "§3 #2｜P0 G2-1｜§7 #7",
               "已落地：store.py 改调 owner 的 append_in_session；三道保护同一份实现；白名单 1 个文件"),
        MapRow("不匹配", "#3 事件类型语义（关键词意图烧进 event_type；清单 4 份）",
               "降级为证据", "L0 事实底座 · 只产证据（metadata.intent_hint）", "§3 #3｜P0 G2-3",
               "结论不进 append-only 事实行"),
        MapRow("匹配", "#5 身份 / 权威 / 可见性（A 区最强项）",
               "合并", "L0 事实底座 · 身份与权威", "§3 #5｜A §5.1",
               "仅关系标签两套映射、membership key 5 处待收编"),
        MapRow("匹配", "#6 时区 / 时钟（24 处已收口，G3 AST 守卫已绿）",
               "合并", "L0 事实底座 · 时序", "§3 #6｜架构所有权地图:187/152",
               "2 个漏点待补（legacy._parse_datetime、8 个 utc* 假名）"),
        MapRow("不匹配", "#7 库 schema / 迁移（14 张同名表；两套迁移机制）",
               "新增结构", "L0 事实底座 · parity 守卫", "§3 #7｜P0 G3-9",
               "同名表先加 PRAGMA parity 守卫（§6 P0）"),
        MapRow("不匹配", "#8 写入权威（threads 6 写方 / speech store 直写 11 表）",
               "收编", "L0 事实底座 · 一表一 owner", "§3 #8｜P1 G2-5/G2-7",
               "先定概念再定表；登记或下沉 db/repo_speech.py"),
        MapRow("部分", "#9 仓储层定位（7 处业务判定藏在仓储里）",
               "收编", "L0 事实底座 · 判定上移 core", "§3 #9｜P1 G1-4",
               "仓储只存取；分层倒挂补守卫（G1-5）"),
        # ── 理解 / 决策 ────────────────────────────────────────────────
        MapRow("匹配", "#10 消息归属（边）｜#11 代词绑定",
               "合并", "L1 归属与指代（已达标）", "§3 #10/#11",
               "只需把 5 份 pat 正则、2 份引用头、7 份问句判定收编回来"),
        MapRow("不匹配", "#12 话题簇 / 锚点（判定 owner 不存在；3 组近似实现）",
               "新增结构", "L4 N3 焦点状态（session_focus 单表已建、0 行）", "§3 #12｜P0 G2-2｜§7 #4",
               "P0-WP3 已落：repo_focus.py + 迁移 54；聚类内核与消费方注入属 P1"),
        MapRow("部分", "#13 收件人信号打分（生产路径默认不可达）",
               "降级为证据", "L2 N1 意图层 · 打分器", "§3 #13｜C §4-10",
               "第 5 份问句词表易被误当线上口径"),
        MapRow("不匹配", "#14 准入（5 道闸门默认态互斥；触发路径一处都不判）",
               "合并", "L2 N1 意图层 · 准入合并进同一次调用", "§3 #14｜§6 P1",
               "净增 0 调用（架构评估:423）；缺口 G7 守卫要补"),
        MapRow("不匹配", "#15 意图判断 N1（契约已建成，判定仍分散、未接线）",
               "新增结构", "L2 N1 意图层 · 大 LLM 唯一 owner", "§3 #15｜P0 G1-1/G1-2/G1-6｜§7 #1",
               "P0-WP1 已落 reply_intent.py 551 行 + 13 用例；接线属 P1（flag 默认 shadow、无读者）"),
        MapRow("不匹配", "#16 规划-执行 N2（顺序有了、三字段没有）",
               "新增结构", "L3 N2 规划-执行 · 外层步骤 + 内层对比", "§3 #16｜R-C1..C7",
               "包裹不是替换；日常回合要能进去"),
        MapRow("不匹配", "#17 参数收集 N5（完全不存在）",
               "新增结构", "L7 N5 参数状态机 · 收集→校验→引用+确认→执行", "§3 #17｜R-P1..P8",
               "现状仅一次「我准备这样做…可以继续吗？」"),
        MapRow("不匹配", "#24 出站闸门定位（只观测已落地）",
               "降级为证据", "L6 生成与人格 · 闸门只观测（LOG_ONLY，P0-WP2 已落地）",
               "§3 #24｜P0 G4-1｜§7 #1/#14",
               "禁语/字数/断言恒 observe；重复指纹+沉默熔断保留；观测项/拦截项拆两 flag"),
        MapRow("部分", "#23 出稿与语域（同一症状两个 owner；额外一次 LLM）",
               "收编", "L6 生成与人格 · 语言实现单点", "§3 #23｜P1 G1-8",
               "conversational 一行不改（方案 A）"),
        # ── 动作 / 规则 / 工具 / 台账 ───────────────────────────────────
        MapRow("不匹配", "#27 动作权限与前置（12 处校验点，5 处工具内各写一份）",
               "新增结构", "L5 N4 规则引擎 · 唯一判定点", "§3 #27｜P0 G1-13",
               "max_calls/require_confirmation 接进或删，不许悬着"),
        MapRow("不匹配", "#28 规则引擎 N4（只作用于「说不说」；缺四样）",
               "新增结构", "L5 N4 规则引擎 · 五维 + Web UI", "§3 #28｜R-V2",
               "复用 policy_rules + store.get_revision()，改数据不重启"),
        MapRow("部分", "#29 任务台账（缺谁下达 / 取消改期 / 读取面 / 迁移）",
               "合并", "L7 N5 参数状态机 · work_items 台账", "§3 #29｜P1 G5-16/G5-17",
               "权威侧是 reminders；todos 收敛成一个 kind"),
        MapRow("不匹配", "#30 调度可观测（无 job 级静默死掉检测）",
               "收编", "L5 N4 规则引擎 · job 健康面", "§3 #30｜P1 G5-19",
               "_JOB_STATE → /api/status + 连续失败通知"),
        MapRow("部分", "#31 配置面（Web 写 .env key 拼错；check_reload 零调用方）",
               "收编", "L5 N4 规则引擎 · 白名单键 + 可验证生效", "§3 #31｜P1 G4-5/G4-6",
               "UI 明说「配置需重启、规则不需」"),
        MapRow("部分", "#32 flag 口径（5 个恒真开关已收编为三态，P0-WP4）",
               "退役", "L0 事实底座 · flag 三态统一（已落地）", "§3 #32｜P0 G4-2｜§7 #7",
               "已落地：flag 名集中注册在 core/features.py，四处只做别名；默认 on、=off 才关"),
        MapRow("不匹配", "#33 LLM 客户端（无统一结构化输出校验）",
               "新增结构", "L2 N1 意图层 · llm/schema.py 校验", "§3 #33｜P0 G5-15",
               "禁止 _raw 透传；直接挡 R-A2「合法率 = 100%」"),
        MapRow("部分", "#34 外部接口 Web（三条安全门槛已修，P0-WP5）",
               "收编", "L9 平台执行 · 本地控制台主体（安全三门槛已落地）",
               "§3 #34｜P0 G5-10/11/12｜§7 #11/#12",
               "已落地：任意路径读文件（媒体根+扩展名白名单，越界 404）· /media/* 纳入鉴权前缀 · 管理面伪造 owner 非本机 403 + 审计"),
        MapRow("部分", "#35 评测面（意图/规划类指标均未建；循环论证）",
               "新增结构", "L2 N1 意图层 / L3 N2 规划-执行 · 两路质量对比读数", "§3 #35｜R-C7 硬条款",
               "没有对比读数就不许宣称快路径合格"),
        # ── 记忆 / 人物 ────────────────────────────────────────────────
        MapRow("不匹配", "#19 记忆写入（投影有第二 / 第三写方）",
               "收编", "L10 记忆层 · 投影写点 3 → 1", "§3 #19｜P1 G2-8",
               "owner 暴露 supersede；每条记忆可回溯源事件"),
        MapRow("部分", "#20 记忆范围不变量 R-I1/I3（行级强制已落地）",
               "新增结构", "L10 记忆层 · 可见性下沉数据层（P0-WP5 已落地）", "§3 #20｜P0 G3-2｜§7 #11/#12",
               "list_for_person 行级 scope + fail-closed；越界被拒用例 8/8"),
        MapRow("不匹配", "#21 人物画像（整链线上是死的，纠错不对称）",
               "新增结构", "L10 记忆层 · 四层在线纠正", "§3 #21｜E §重点①",
               "notes / observations / profiles / proposals 补纠正入口"),
        MapRow("部分", "#18 记忆召回（两条打分链 + 降级不可见）",
               "收编", "L10 记忆层 · 共用打分函数", "§3 #18｜P1 G2-10",
               "同一 0.45 硬编码两次；self 权重 1.00 vs 0.95"),
        # ── 平台执行 / 发送 ────────────────────────────────────────────
        MapRow("部分", "#25 发送执行（send_log 无幂等键；3 处假成功）",
               "新增结构", "L8 出站边界 · 幂等键 + 回读验证", "§3 #25｜P1 G5-1/G5-2/G5-3",
               "补齐前绝不在「先发后写」之间插入重试逻辑"),
        MapRow("部分", "#26 平台适配（一个平台两条架构）",
               "合并", "L8 出站边界 / L9 平台执行 · IntentBus vs Provider", "§3 #26｜P2 G5-7",
               "QQ 710 行协议可作样板"),
        MapRow("不匹配", "#4 说话人解析（仍有 1 处自写，判据不等价）",
               "收编", "L0 事实底座 · 改调 speaker owner", "§3 #4｜P0 G2-9",
               "守卫扫根加 core/terminal/"),
        MapRow("部分", "#22 人格 / 语气（群聊规则文本 3 处各写已漂）",
               "收编", "L6 生成与人格 · persona 单处", "§3 #22｜P1",
               "块清单 3 处来源；删 conversational.py 里的规则文本副本"),
        MapRow("不匹配", "旧实现（CoarseAssigner 281 行 / task_session 69 行 / signals 死字段 / 旧 ContentGuard / topic_line 自算话题）",
               "退役", "L3/L4/L8 旧近似实现整体退役（灰：画成虚线框）", "§2.3｜P2 动作清单",
               "退役公共前置三条：静态守卫先立 / 全绿 + golden diff 可解释 / 删完指标不降"),
        MapRow("匹配", "横向底盘（事实 / 身份 / 时序 / 归属 / 绑定 / 出站网关 / 账本）",
               "合并", "L0/L1/L8 保留且已收口（绿）", "§0.0｜架构评估:15",
               "§3 匹配 4 行全部是横向底盘；骨架不需要拆"),
    ]


BANNER_H = 138
BOTTOM_H = 158


def draw_fig3_body(c: Canvas, width: float) -> float:
    d = c.d
    y = title_block(
        c, "现状 - 目标对照（右列＝统一层名 L0–L10）",
        "要动的是纵向能力层与边界面，不是骨架",
        [("匹配 4", C_MATCH), ("部分 15", C_PART), ("不匹配 16", C_P0),
         ("层名 11（L0–L10）", C_NEW), (P0_FIXED_BADGE, C_FIXED)],
        width,
    )
    LEFT_W, MID_W, RIGHT_W = 690, 132, 590
    x_left = M
    x_mid = x_left + LEFT_W
    x_right = x_mid + MID_W
    pad_in = 16
    f_left, f_det = font(19, bold=True), font(15)
    f_right, f_cite, f_act = font(19, bold=True), font(15), font(16, bold=True)

    rows = map_rows()
    heights: list[float] = []
    for r in rows:
        ll = wrap(d, r.left, f_left, LEFT_W - pad_in * 2 - 8)
        dl = wrap(d, r.detail, f_det, LEFT_W - pad_in * 2 - 8)
        rl = wrap(d, r.right, f_right, RIGHT_W - pad_in * 2)
        cl = wrap(d, r.cite, f_cite, RIGHT_W - pad_in * 2)
        h_l = 16 + len(ll) * 25 + (len(dl) * 20 + 2 if dl else 0) + 14
        h_r = 16 + len(rl) * 25 + len(cl) * 21 + 14
        heights.append(max(h_l, h_r, 88))

    # 顶部横幅
    by = y + 12
    d.rounded_rectangle([M, by, width - M, by + BANNER_H], radius=16, fill="#F4F7FB",
                        outline=C_NEW, width=3)
    d.text((M + 26, by + 32), "逐层对照实测（docs/全链路架构审计与目标架构.md §3，35 行）",
           font=font(23, bold=True), fill=INK, anchor="lm")
    chip_y = by + 92
    cx = M + 26
    for label, value, color in [("匹配", "4 行", C_MATCH), ("部分", "15 行", C_PART),
                                ("不匹配", "16 行", C_P0)]:
        w = 168
        d.rounded_rectangle([cx, chip_y - 27, cx + w, chip_y + 27], radius=10,
                            fill=FILLS[color], outline=color, width=3)
        d.text((cx + 18, chip_y), label, font=font(21, bold=True), fill=color, anchor="lm")
        d.text((cx + w - 18, chip_y), value, font=font(23, bold=True), fill=color, anchor="rm")
        cx += w + 16
    d.text((cx + 14, chip_y - 13), "读法（§3 原文）：匹配的全部是「横向底盘」，",
           font=font(19, bold=True), fill=INK_SOFT, anchor="lm")
    d.text((cx + 14, chip_y + 15), "不匹配的全部是「纵向能力层 + 边界面」→ 要动的是纵向能力层与边界面，不是骨架。",
           font=font(19, bold=True), fill=C_P0, anchor="lm")

    # 表头
    hy = by + BANNER_H + 16
    d.rounded_rectangle([x_left, hy, x_left + LEFT_W, hy + 38], radius=9, fill=C_PART)
    d.text((x_left + pad_in, hy + 19), "左：现状（§3 能力层 / 现状 owner）",
           font=font(18, bold=True), fill="#FFFFFF", anchor="lm")
    d.rounded_rectangle([x_mid + 8, hy, x_mid + MID_W - 8, hy + 38], radius=9, fill=C_ARROW)
    d.text((x_mid + MID_W / 2, hy + 19), "动作", font=font(18, bold=True), fill="#FFFFFF", anchor="mm")
    d.rounded_rectangle([x_right, hy, x_right + RIGHT_W, hy + 38], radius=9, fill=C_NEW)
    d.text((x_right + pad_in, hy + 19), "右：目标（统一层名 L0–L10，见 §0.4 映射表）",
           font=font(18, bold=True), fill="#FFFFFF", anchor="lm")

    yy = hy + 38 + 14
    row_geom: list[tuple[MapRow, float, float]] = []
    for r, h in zip(rows, heights):
        row_geom.append((r, yy, yy + h))
        yy += h + 10

    # 第 1 遍：左列 + 右列（先铺底，避免后面被箭头压住）
    for r, y0, y1 in row_geom:
        status_color = {"匹配": C_MATCH, "部分": C_PART, "不匹配": C_P0}[r.status]
        d.rounded_rectangle([x_left, y0, x_left + LEFT_W, y1], radius=11,
                            fill=FILLS[C_P0] if r.status == "不匹配" else FILLS[C_PART],
                            outline=status_color, width=2)
        d.rounded_rectangle([x_left, y0, x_left + 8, y1], radius=4, fill=status_color)
        ty = y0 + 14
        for ln in wrap(d, r.left, f_left, LEFT_W - pad_in * 2 - 8):
            d.text((x_left + pad_in + 8, ty), ln, font=f_left, fill=INK, anchor="la")
            ty += 25
        if r.detail:
            ty += 2
            for ln in wrap(d, r.detail, f_det, LEFT_W - pad_in * 2 - 8):
                d.text((x_left + pad_in + 8, ty), ln, font=f_det, fill=INK_MUTED, anchor="la")
                ty += 20
        # 右列：退役走灰色虚线框（灰 = 死代码 / 退役），其余走蓝色实线框
        if r.action == "退役":
            d.rounded_rectangle([x_right, y0, x_right + RIGHT_W, y1], radius=11,
                                fill=FILLS[C_DEAD])
            dashed_rrect(d, [x_right, y0, x_right + RIGHT_W, y1], 11, C_DEAD, 3)
        else:
            d.rounded_rectangle([x_right, y0, x_right + RIGHT_W, y1], radius=11,
                                fill=FILLS[C_NEW], outline=C_NEW, width=2)
        ty = y0 + 14
        for ln in wrap(d, r.right, f_right, RIGHT_W - pad_in * 2):
            d.text((x_right + pad_in, ty), ln, font=f_right,
                   fill=INK if r.action != "退役" else INK_SOFT, anchor="la")
            ty += 25
        ty += 2
        for ln in wrap(d, r.cite, f_cite, RIGHT_W - pad_in * 2):
            d.text((x_right + pad_in, ty), ln, font=f_cite,
                   fill=C_NEW if r.action != "退役" else C_DEAD, anchor="la")
            ty += 21

    # 第 2 遍：中间动作箭头（画在两侧框之上，箭头尖才看得见）
    for r, y0, y1 in row_geom:
        mid = (y0 + y1) / 2
        acolor = ACTION_COLORS.get(r.action, C_ARROW)
        poly_arrow(d, [(x_mid + 6, mid), (x_right - 10, mid)], acolor, 3, head=14,
                   dashed=(r.action == "退役"))
        aw = text_w(d, r.action, f_act) + 20
        d.rounded_rectangle([x_mid + MID_W / 2 - aw / 2, mid - 15,
                             x_mid + MID_W / 2 + aw / 2, mid + 15],
                            radius=8, fill=PAGE, outline=acolor, width=2)
        d.text((x_mid + MID_W / 2, mid + 1), r.action, font=f_act, fill=acolor, anchor="mm")

    # 底部 P0 三项（§6 P0「动作清单」里主人先拍的那三项）——**交付状态照实标**：
    # ①②③ 都已在本轮 P0 落地（见 output/commit_p0_wp1/wp24/wp5.txt），
    # 所以框边与标题用青绿（C_FIXED），**不再让主人以为还欠着**。
    fy = yy + 12
    d.rounded_rectangle([M, fy, width - M, fy + BOTTOM_H], radius=16, fill=FILLS[C_FIXED],
                        outline=C_FIXED, width=3)
    d.text((M + 26, fy + 30), "P0 三项（§6 P0 动作清单 / §7 拍板项 1、6）——已全部落地",
           font=font(22, bold=True), fill=C_FIXED, anchor="lm")
    items = [
        ("① 意图层契约 √已落地",
         "reply_intent.py 551 行 + 13 用例；纯结构、0 次新增 LLM 调用；**接线属 P1**｜P0 G2-3 前置、G1-1/2/6"),
        ("② 闸门降为只观测 √已落地",
         "禁语/字数/断言恒 observe（文本逐字不动）；重复指纹+沉默熔断保留；观测项/拦截项拆两个 flag（§7 #1/#14）｜P0 G4-1"),
        ("③ 记忆范围补数据层强制 √已落地",
         "list_for_person 行级 scope + fail-closed；越界守卫用例 8/8（R-I3 硬验收项）｜P0 G3-2"),
    ]
    ix = M + 26
    iw = (width - 2 * M - 52 - 2 * 18) / 3
    for title, body in items:
        d.rounded_rectangle([ix, fy + 52, ix + iw, fy + BOTTOM_H - 14], radius=10,
                            fill=PAGE, outline=C_FIXED, width=2)
        d.text((ix + 14, fy + 74), title, font=font(19, bold=True), fill=C_FIXED, anchor="lm")
        for i, ln in enumerate(wrap(d, body, font(15), iw - 28)):
            d.text((ix + 14, fy + 100 + i * 20), ln, font=font(15), fill=INK_SOFT, anchor="la")
        ix += iw + 18
    return fy + BOTTOM_H


def draw_fig3() -> tuple[Path, tuple[int, int, int]]:
    probe = Canvas(W3, PROBE_H)
    y_end = draw_fig3_body(probe, W3)
    total = y_end + 30 + 2 * 25 + 40
    c = Canvas(W3, total)
    draw_fig3_body(c, W3)
    footer(c, total - 96, W3,
           "口径：本图是 P0 交付后的状态（刷新日期 2026-10-01）。"
           "§3 的状态列照旧是实测读数（匹配 = 与 R-* 及 N1–N5 同向且已有守卫；部分 = 方向对但缺 owner/契约/覆盖；"
           "不匹配 = 与契约正面冲突或 owner 不存在）——本轮 P0 落地不改状态列，"
           "只在左列/右列/细节里标出哪几行已经动过（「√已落地（P0）」字样 + 底部三项）。"
           "中间箭头上的动作 ∈ 新增结构 / 收编 / 退役 / 降级为证据 / 合并（照 §2.3 与 §6 的动作分类）。")
    p = OUT_DIR / "现状-目标对照.png"
    return p, c.save(p)


# ===========================================================================
# 中文渲染自检（硬性要求：必须验证中文不是方块）
# ===========================================================================
def _ink_ratio(ch: str, size: int = 72) -> float:
    """单字墨量占比。真字形笔画不同 → 墨量不同；豆腐块 / .notdef 会高度雷同。"""
    im = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(im).text((size // 2, size // 2), ch, font=font(size, bold=True), fill=255)
    data = im.tobytes()
    return sum(1 for b in data if b > 40) / float(len(data))


def _glyph_bitmap(ch: str, size: int = 72) -> bytes:
    """把单个字符渲染到定长 L 画布再取字节（用于"是不是 .notdef 豆腐块"的位图比对）。"""
    im = Image.new("L", (size * 3, size * 2), 0)
    ImageDraw.Draw(im).text((8, 8), ch, font=font(size, bold=True), fill=255)
    return im.tobytes()


def verify_glyphs() -> dict[str, object]:
    """**图里真正用到的非 ASCII 记号**必须有字形（不能是 .notdef 豆腐块）。

    为什么单独守这一条：``d.textbbox`` 对缺字字符**也会返回一个方框尺寸**
    （.notdef 本身就是个方框），所以"包围盒不为空"分不出「有字形」和「豆腐块」。
    唯一的可靠判据是**位图比对**：与确定缺字的私用区码位（U+E000）位图完全相同
    = .notdef。踩过的坑：``✓``(U+2713) 在 ``msyh.ttc`` 里**没有字形**，
    图例第一版就渲成了方框；换成 ``√``(U+221A) 才有真字形。
    """
    missing_ref = _glyph_bitmap("\ue000")   # 私用区：必然无字形
    probe = [c for c in SIGIL_PROBE if c.strip()]
    bad = [c for c in probe if _glyph_bitmap(c) == missing_ref]
    return {
        "glyph_probe": probe,
        "missing_glyph": bad,
        "ok": not bad,
    }


def verify_cjk() -> dict[str, object]:
    """验证中文不是方块：
    1) 每个待测汉字的墨迹包围盒必须接近全角字框（不是空框 / 缺字）；
    2) 不同汉字的墨量必须有差异（豆腐块会完全一致）；
    3) 全角 advance ≈ size、半角明显更小、空格有前进宽度；
    4) 图里用到的非 ASCII 记号（√ 等）必须有字形（位图比对，见 :func:`verify_glyphs`）。
    """
    probe = Image.new("L", (1400, 200), 0)
    d = ImageDraw.Draw(probe)
    sample = "中文渲染自检：现状架构全景 / 目标架构全景 / 现状-目标对照（P0 已修 5 项）"
    f = font(40, bold=True)
    d.text((10, 20), sample, font=f, fill=255)

    adv_cjk = d.textlength("意", font=f)
    adv_ascii = d.textlength("A", font=f)
    adv_space = d.textlength(" ", font=f)
    box = d.textbbox((0, 0), sample, font=f)
    rendered_w = box[2] - box[0]

    cjk_probe = "意图层架构现状目标对照"
    ink = {ch: round(_ink_ratio(ch), 4) for ch in cjk_probe}
    bb_w, empty = [], []
    for ch in cjk_probe:
        bb = d.textbbox((0, 0), ch, font=font(72, bold=True))
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        bb_w.append(round(w, 1))
        if w < 40 or h < 40:
            empty.append(ch)
    distinct_ink = len(set(ink.values()))
    ink_spread = max(ink.values()) - min(ink.values())
    inked_rows = sum(
        1 for y in range(20, 160)
        if max(probe.getpixel((x, y)) for x in range(10, 900, 3)) > 60
    )

    ok = (not empty
          and distinct_ink >= 5
          and ink_spread >= 0.03
          and 0.85 <= adv_cjk / 40.0 <= 1.05
          and 0.35 <= adv_ascii / 40.0 <= 0.80
          and adv_space > 0
          and rendered_w > 600
          and inked_rows >= 40)
    sigils = verify_glyphs()
    ok = ok and bool(sigils["ok"])
    return {
        "font": str(FONT_PATH),
        "font_name": font(20).getname(),
        "ok": ok,
        "adv_cjk(意)/size": round(adv_cjk / 40.0, 3),
        "adv_ascii(A)/size": round(adv_ascii / 40.0, 3),
        "adv_space": round(adv_space, 1),
        "sample_px_w": round(rendered_w, 1),
        "cjk_bbox_w(应≈全角)": bb_w,
        "ink_ratio(11字)": ink,
        "distinct_ink": distinct_ink,
        "ink_spread": round(ink_spread, 4),
        "inked_rows": inked_rows,
        "empty_or_missing_glyph": empty,
        "sigil_glyph_missing(位图比对)": sigils["missing_glyph"],
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 78)
    print(f"字体：{FONT_PATH}")
    v = verify_cjk()
    for k, val in v.items():
        print(f"  自检 {k:20} = {val}")
    print(f"  中文渲染判定：{'通过（中文不是方块）' if v['ok'] else '失败（疑似豆腐块 / 缺字）'}")
    if not v["ok"]:
        print("  !! 中文字形自检未通过，终止出图。", file=sys.stderr)
        return 2

    print("=" * 78)
    results = [draw_fig1(), draw_fig2(), draw_fig3()]
    for path, (w, h, size) in results:
        print(f"已写出 {path.relative_to(REPO)}")
        print(f"   像素 {w} x {h}   文件 {size:,} 字节 ({size / 1024:.1f} KiB)")
    print("=" * 78)
    print(f"共 {len(results)} 张，全部幂等覆盖 {OUT_DIR.relative_to(REPO)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
