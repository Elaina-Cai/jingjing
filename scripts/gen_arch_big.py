# -*- coding: utf-8 -*-
"""群聊社会认知 融合大图（v5）：内部件收进模块框（主干容器 + 支撑容器）。"""
from __future__ import annotations

import html
import pathlib
import xml.dom.minidom

W, H = 4400, 2200
INK, MUTED = "#263238", "#546e7a"
FILL = {"green": "#e8f5e9", "yellow": "#fff8e1", "blue": "#e3f2fd", "purple": "#f3e5f5", "cyan": "#e0f7fa", "grey": "#eceff1"}
STROKE = {"green": "#66bb6a", "yellow": "#ffca28", "blue": "#64b5f6", "purple": "#ba68c8", "cyan": "#4dd0e1", "grey": "#b0bec5"}
CHIP_C = {"on": "#2e7d32", "影子": "#f9a825", "待": "#c62828", "混合": "#6a1b9a"}
EDGE, E_BLUE, E_PURPLE, E_CYAN, E_GREEN = "#90a4ae", "#1e88e5", "#8e24aa", "#00897b", "#43a047"
E_ORANGE = "#ef6c00"
out = []


def esc(s):
    return html.escape(str(s), quote=True)


def add(s):
    out.append(s)


def rect(x, y, w, h, fill="#ffffff", stroke="#b0bec5", rx=10, sw=1.5, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>'


def txt(x, y, s, size=14, fill=INK, anchor="start", weight="normal"):
    return ('<text x="%s" y="%s" font-family="Microsoft YaHei,PingFang SC,sans-serif" font-size="%s" '
            'fill="%s" text-anchor="%s" font-weight="%s">%s</text>' % (x, y, size, fill, anchor, weight, esc(s)))


def chip(x, y, s):
    c = CHIP_C.get(s, "#546e7a")
    w = 16 + 13 * len(s)
    return rect(x, y - 14, w, 21, fill=c, stroke=c, rx=10, sw=0) + txt(x + w / 2, y + 1, s, size=12, fill="#ffffff", anchor="middle", weight="bold")


def wrap(s, n):
    s = str(s)
    chunks = [s[i:i + n] for i in range(0, len(s), n)]
    if len(chunks) > 2:
        chunks = chunks[:2]
        chunks[1] = chunks[1][: max(1, n - 3)] + "..."
    return chunks


def container(x, y, w, h, title, owner, st, col, items, *, card_h=118, gap=14, wrap_n=40):
    add(rect(x, y, w, h, "#ffffff", STROKE[col], 10, 2.5))
    add(rect(x, y, w, 58, FILL[col], STROKE[col], 10, 2.5))
    add(rect(x, y + 46, w, 12, FILL[col], FILL[col], 0, 0))
    add(txt(x + 16, y + 37, title, 19, weight="bold"))
    add(chip(x + w - 104, y + 34, st))
    add(txt(x + 16, y + 86, owner, 12, fill=MUTED))
    cy = y + 100
    for name, note in items:
        add(rect(x + 14, cy, w - 28, card_h, "#f5f7f8", "#cfd8dc", 8, 1))
        add(txt(x + 26, cy + 30, name, 15, weight="bold"))
        for k, line in enumerate(wrap(note, wrap_n)[:2]):
            add(txt(x + 26, cy + 56 + k * 20, line, 12, fill=MUTED))
        cy += card_h + gap


MAIN = [
    ("① 感知", "core/context/perception.py + scheduler 入站", "on", "green", [
        ("收消息 / 拍 / @ / 引用", "提谁、几点、原文"),
        ("消息事件（①→②）", "message_observed"),
        ("原文账 perception_observed", "tail 200 / text 4000 / quote 200；msg_id 幂等"),
        ("媒体 / 撤回 / 父链", "kind + refs；quote_user / quote_text")]),
    ("② 世界模型（中枢）", "core/context/situation.py + situation_store.py", "on", "purple", [
        ("事件账（唯一跨轮持久）", "message / perception / emotion / fact / position / episode / self_message"),
        ("四投影", "事实 / 位置 / 意图 / 情绪姿态"),
        ("片段对象 Episode", "episode_opened / touched / closed"),
        ("自我投影 self_message", "尾部 50；只记账"),
        ("显著性 salience", "权重 × 半衰期 24h"),
        ("被注入源（给 ③⑤⑥）", "四投影 / 片段 / 显著性 / 情绪姿态"),
        ("落盘恢复", "YINYUE_WORLD_MODEL_DIR")]),
    ("③ 理解（读场 N1）", "core/context/intent.py + scene_read.py", "混合", "green", [
        ("读场一次调用", "roles / targets 集合 / genre / owner_state / updates + speaker"),
        ("归属硬事实", "@ / 引用 / 拍 / 名字"),
        ("N1 语义字段（影子）", "about / thread_continue / takeover_allowed"),
        ("归属仲裁（影子）", "present v2 / 模型提名 / 多目标 / 私聊门"),
        ("准入合并调用", "admission（强指向时）"),
        ("注入点（读上游）", "[局面]（②）/ 窗口·记忆（⑨）/ 成员名单（⑨）"),
        ("N1 对账 n1_arbitration", "about 冲突 / 规则读数")]),
    ("④ 情绪", "core/context/emotion.py", "影子", "yellow", [
        ("行为信号 + 理解证据", "initiated / replied / waited；维护 / 晾着 / 起哄"),
        ("积分与衰减", "半衰期 12h；起因 doctrine"),
        ("情绪分层", "mood（48h）/ attitude / tone"),
        ("只算不存", "状态回写②")]),
    ("⑤ 意图（说 / 不说 / 做）", "core/routing/*", "混合", "green", [
        ("正经事信号", "@ / 委托 / 提问 / 承诺 / 撤回风险 / 时间压力"),
        ("说 / 不说 / 做", "沉默也是意图"),
        ("预算状态", "→②（转 on 前定）或可丢缓存"),
        ("路径约定", "回答 / 主动 / 不说")]),
    ("⑥ 行动（规划 + 表达）", "agents/planner.py + conversational.py", "混合", "green", [
        ("手段-目的规划", "PLAN_LOOP（FAST_PATH_FROM_INTENT=on）"),
        ("只读工具执行", "execute_attached_plan + 白名单"),
        ("人设读取", "persona_channel（经⑩）"),
        ("表达装配", "[本轮理解] / [本轮步骤] / 语气 / 人话（待）"),
        ("注入点（读上游）", "[本轮理解]（⑤）/ [情绪语气]（④）/ 人设（⑩）/ 窗口·记忆（⑨）"),
        ("人话工作流（待规格）", "voice 重写 / 风格影子 / 语感样本 / 节奏"),
        ("工具循环", "工具结果回灌")]),
    ("⑦ 兜底（出口闸门）", "core/speech/outbound.py", "混合", "yellow", [
        ("出口闸门 OUTPUT_GATE", "on"),
        ("@ 守卫 + @ 渲染", "guard shadow / from_intent on"),
        ("语气 / 情绪读数", "observe-only"),
        ("结果与自我消息回写②", "self_message"),
        ("出口形态审计（待）", "模型味负特征检查（只读）")]),
]

SUPPORT = [
    ("③ 判定件 · 话题簇 / 归属（⑤ 消费）", "core/context/threads.py（判定件，非独立模块；不持状态）", "判定件", "blue", [
        ("话题簇 / 焦点", "60s 间隙 + 共享 2-gram"),
        ("回合归属（about_me）", "me / not_me / 空"),
        ("unit_continuation", "clarify / 跳过判据"),
        ("对齐改造 1A+2A", "anchor_changed / takeover_allowed / about"),
        ("THREADS 达标", "错合率 0.2277（目标 ≤ 0.10）")]),
    ("⑨ 记忆（底座）", "core/terminal/store_port.py + db/* + consolidation.py", "混合", "purple", [
        ("终端事件 / 记忆投影", "memory_projections"),
        ("关系 / 人设 / 提醒", "经⑩读写"),
        ("消息窗口 chat_window", "40 条原文 + 压缩"),
        ("巩固环 consolidation（影子）", "② 高显著 → ⑨ 幂等写入"),
        ("统一读写口 store_port", "meta / account / adm / unified")]),
    ("⑩ 工具包（纯通道）", "core/execution/* + agents/tools/*", "on", "cyan", [
        ("发送", "outbound 网关 / wx"),
        ("记忆检索", "recall_for_context"),
        ("查人 / 搜索 / 读人设", "persona_channel"),
        ("提醒 / 状态落盘恢复", "data_channels 三面"),
        ("通道纪律", "不写业务逻辑、状态归②")]),
    ("跨层判定件 · 节奏 / 时间", "core/timing.py（判定件，非模块）", "判定件", "grey", [
        ("节奏单 owner", "coalesce / pat grace / stale / merge"),
        ("窗口参数", "room / qa gap / duplicate / ownership"),
        ("不持状态", "纯参数读取；不写库、不缓存"),
        ("覆盖范围", "coalesce / pat grace / stale / merge / room / qa / duplicate"),
        ("回滚", "改 .env → 重启")]),
    ("运行状态", "logs / scripts / docs", "混合", "grey", [
        ("影子清单 + 收数脚本", "report_* 系列"),
        ("官方全量 runner", "269 文件；4 项既有红"),
        ("同步纪律", "新增对象 / 跨层 owner / 闭环边 → 两图同批更新")]),
]

add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
add('<defs>')
for c in (EDGE, E_BLUE, E_PURPLE, E_CYAN, E_GREEN, "#37474f", E_ORANGE):
    add(f'<marker id="m{c[1:]}" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6 z" fill="{c}"/></marker>')
add('</defs>')
add(rect(0, 0, W, H, fill="#fafafa", stroke="#fafafa", rx=0, sw=0))
add(txt(40, 52, "群聊社会认知 · 全景 + 模块内部（融合版 v5）", size=30, weight="bold"))
add(txt(40, 84, "主干容器里直接嵌模块内部件；支撑层同样内嵌。状态芯片 on / 影子 / 待 / 混合；虚线=回写/判定/工具调用。", size=15, fill=MUTED))
add(txt(40, 114, "主干：① 感知 → ② 世界模型（中枢）→ ③ 理解 → ④ 情绪 → ⑤ 意图 → ⑥ 行动 → ⑦ 兜底", size=16, weight="bold"))

mw, mgap = 560, 34
mx0 = (W - (7 * mw + 6 * mgap)) / 2
my, mh = 150, 1120
centers = []
for i, (t, o, st, col, items) in enumerate(MAIN):
    x = mx0 + i * (mw + mgap)
    centers.append(x + mw / 2)
    container(x, my, mw, mh, t, o, st, col, items, card_h=122, gap=14, wrap_n=38)

sw_, sgap = 683, 24
sx0 = (W - (6 * sw_ + 5 * sgap)) / 2
sy, sh = 1390, 620
scenters = []
for i, (t, o, st, col, items) in enumerate(SUPPORT):
    x = sx0 + i * (sw_ + sgap)
    scenters.append(x + sw_ / 2)
    container(x, sy, sw_, sh, t, o, st, col, items, card_h=92, gap=10, wrap_n=50)

# 全量运作流程（对齐 模块流程图.svg 的边集；虚线=回写/判定/工具/巩固）
GREY = "#37474f"


def edge(points, color, dash="7 5", sw=1.8):
    d = "M " + " L ".join(f"{x} {y}" for x, y in points)
    da = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<path d="{d}" stroke="{color}" stroke-width="{sw}" fill="none"{da} marker-end="url(#m{color[1:]})"/>'


def lab(x, y, s, color, rotate=False):
    tr = f' transform="rotate(-90 {x} {y})"' if rotate else ""
    return f'<text x="{x}" y="{y}" font-family="Microsoft YaHei,PingFang SC,sans-serif" font-size="12.5" fill="{color}"{tr}>{esc(s)}</text>'


FLOW = [
    ([(418, 120), (418, 146)], GREY, "", (430, 134, "新消息（含主人裁断回复）", False)),
    ([(700, 710), (728, 710)], GREY, "", (712, 720, "消息事件", True)),
    ([(1294, 710), (1322, 710)], GREY, "", (1306, 720, "事实/投影（读）", True)),
    ([(1888, 710), (1916, 710)], GREY, "", (1900, 720, "理解+情绪事件", True)),
    ([(2482, 710), (2510, 710)], GREY, "", (2494, 720, "情绪+理解+roles/tau", True)),
    ([(3076, 710), (3104, 710)], GREY, "", (3088, 720, "意图+许可", True)),
    ([(3670, 710), (3698, 710)], GREY, "", (3682, 720, "待发内容", True)),
    ([(4262, 710), (4330, 710), (4330, 120), (4210, 120)], GREY, "", (4272, 700, "回复/行动（出图）", False)),
    ([(1606, 1270), (1606, 1300), (1012, 1300), (1012, 1276)], E_PURPLE, "7 5", (1618, 1292, "world_updates 写回（事实/意图/情绪）", False)),
    ([(1012, 150), (1012, 128), (2200, 128), (2200, 150)], E_PURPLE, "7 5", (1030, 124, "读投影（计算用）", False)),
    ([(2200, 150), (2200, 136), (1012, 136), (1012, 150)], E_PURPLE, "7 5", (1030, 148, "情绪状态回写（仍归②）", False)),
    ([(3982, 150), (3982, 120), (1070, 120), (1070, 150)], E_PURPLE, "7 5", (3000, 114, "结果/自我消息（下一轮经验）", False)),
    ([(1012, 1270), (1012, 1355), (2553, 1355), (2553, 1384)], E_PURPLE, "7 5", (1024, 1348, "状态落盘/恢复", False)),
    ([(1012, 1270), (1012, 1320), (1139, 1320), (1139, 1384)], E_BLUE, "8 5", (1024, 1313, "pending 假说池（③此前产出）", False)),
    ([(1139, 1390), (1139, 1335), (1075, 1335), (1075, 1276)], E_BLUE, "8 5", (1151, 1378, "confirmed / contradicted", False)),
    ([(1606, 150), (1606, 144), (2794, 144), (2794, 150)], E_BLUE, "8 5", (1618, 140, "理解+情绪→意图（roles/tau）", False)),
    ([(45, 120), (45, 1650), (790, 1650)], E_BLUE, "8 5", (55, 1000, "主人裁断回复（经①识别）", True)),
    ([(798, 1690), (75, 1690), (75, 122), (700, 122)], E_BLUE, "8 5", (85, 560, "批式私聊主人（编号/48h）", True)),
    ([(1606, 1270), (1606, 1340), (2620, 1340), (2620, 1384)], E_CYAN, "7 5", (1618, 1333, "记忆召回/检索", False)),
    ([(3388, 1270), (3388, 1370), (2480, 1370), (2480, 1384)], E_CYAN, "7 5", (3400, 1363, "工具循环", False)),
    ([(3982, 1270), (3982, 1285), (2553, 1285), (2553, 1384)], E_CYAN, "7 5", (3994, 1278, "发送调用", False)),
    ([(1139, 2010), (1139, 2032), (2553, 2032), (2553, 2016)], E_CYAN, "7 5", (1149, 2024, "本地证据查询（成员/备注/关系/wxid）", False)),
    ([(2188, 1700), (2212, 1700)], E_CYAN, "7 5", (2202, 1728, "记忆投影/窗口", True)),
    ([(2212, 1730), (2188, 1730)], E_CYAN, "7 5", (2198, 1700, "读写数据库", True)),
    ([(1012, 1270), (1012, 1282), (1846, 1282), (1846, 1384)], E_GREEN, "", (1024, 1276, "巩固环（高显著情节→⑨）", False)),

    ([(1294, 560), (1322, 560)], E_ORANGE, "6 3 2 3", (1306, 570, "注入 [局面]（②投影）", True)),
    ([(2482, 560), (2510, 560)], E_ORANGE, "6 3 2 3", (2494, 570, "注入 情绪/roles", True)),
    ([(3076, 560), (3104, 560)], E_ORANGE, "6 3 2 3", (3088, 570, "注入 [本轮理解]（⑤）", True)),
    ([(1606, 1270), (1606, 1350), (2794, 1350), (2794, 1276)], E_ORANGE, "6 3 2 3", (1618, 1343, "注入 roles/tau（③→⑤）", False)),
    ([(2200, 1270), (2200, 1365), (3340, 1365), (3340, 1276)], E_ORANGE, "6 3 2 3", (2212, 1358, "注入 [情绪语气]（④→⑥）", False)),
    ([(1820, 1390), (1820, 1338), (1606, 1338), (1606, 1276)], E_ORANGE, "6 3 2 3", (1660, 1332, "注入 记忆/窗口（⑨→③）", False)),
    ([(1875, 1390), (1875, 1345), (3388, 1345), (3388, 1276)], E_ORANGE, "6 3 2 3", (1980, 1339, "注入 窗口/记忆（⑨→⑥）", False)),
    ([(2553, 1390), (2553, 1332), (3436, 1332), (3436, 1276)], E_ORANGE, "6 3 2 3", (2600, 1326, "注入 人设（⑩→⑥）", False)),
]
for pts, color, dash, (lx, ly, text, rot) in FLOW:
    add(edge(pts, color, dash))
    add(lab(lx, ly, text, color, rot))
add(txt(40, 2040, "图例：绿=已有能力 / 黄=影子待转正或待改造 / 蓝=新件与内部件 / 紫=中枢与底座 / 青=工具纯通道 / 灰=跨层与运行状态。", size=14, fill=MUTED))
add(txt(40, 2070, "维护：新增对象 / 跨层 owner / 闭环边 → 本图与总览图（模块流程图.svg）同批更新；内部件名以代码为准。", size=14, fill=MUTED))
add(txt(40, 2100, "注：本图由 scripts/gen_arch_big.py 生成（布局即数据表），改图只改数据、一条命令重生成。", size=14, fill=MUTED))
add(txt(40, 2130, "注入 = 橙色点划线（上游产出 → 下游 prompt/判定）；上游只产事实（①②④⑨），下游只消费+回写（③⑤⑥⑦）。", size=14, fill=MUTED))
add(txt(40, 2160, "归属：跨模块只有三类——判定件（不持状态）/ 通道（⑩）/ 状态（②⑨）；新件一律先归模块内部件。", size=14, fill=MUTED))
add('</svg>')

svg = "\n".join(out)
path = pathlib.Path("docs/群聊社会认知_架构图_全景与内部.svg")
path.write_text(svg, encoding="utf-8")
xml.dom.minidom.parseString(svg.encode("utf-8"))
print("written", path, len(svg), "bytes; main:", len(MAIN), "support:", len(SUPPORT))