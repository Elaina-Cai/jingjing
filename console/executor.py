"""律（P2）：唯一的一条"能改东西"的通道。

纪律（写在这里，也写在 docs/律-P2.md）：
  1. 只认白名单：`SCHEMA` 里没有的键，一律拒绝，不做"猜你想改什么"。
  2. 先算 diff，再决定要不要写；`dry_run=True` 时只回 diff，磁盘不动。
  3. 写之前必须留备份（`backups/law/`），写之后必须留审计（`data/console/audit-*.jsonl`）。
  4. 原子写（tmp → os.replace），不接受"写一半"的中间态。
  5. 绝不碰数据库。数据库有它自己的 `db.write_guard`，这里连门都不摸。
     （2026-10-27 唯一例外：账号登记的新增/改/删 **不在这里写**，而是
     `subprocess` 调 `scripts/account_admin.py` —— 由它在自己的进程里声明写意图、
     过 `db.write_guard`、先把整张 `account_registry` 备份成 JSON 再写。
     本模块依旧一行数据库代码都没有。）

它被 `console/collector.py` 的本地 HTTP 端点调用（127.0.0.1:39872 /cmd），
也可以直接 `python -c "from console import executor; print(executor.state())"` 查。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import io
import subprocess
import webbrowser
import socket
import os
import re
import sqlite3
import shutil
import time
from datetime import datetime

import structlog

from core.infra import liveness  # 心跳文件名与判死口径只有一处
from core.infra import duration  # 时长只有一个单位：毫秒（UI-066）
from core.infra.jsonio import atomic_write_json
from core.infra.duration import human  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log = structlog.get_logger("console.executor")  # 2026-11-01：4 处 except 里的 log 从未定义（F821）

try:  # 静默失败要留痕（2026-11-01：这几处 except 里原本用了未导入的 safelog）
    import safelog
except Exception:  # pragma: no cover - 极少见，仓库根不在 sys.path 时
    class _SL:  # type: ignore
        @staticmethod
        def warn(*a, **k):
            pass
    safelog = _SL()

DATA = os.path.join(ROOT, "data")
CONSOLE = os.path.join(DATA, "console")
AUDIT_DIR = CONSOLE
BACKUP_DIR = os.path.join(ROOT, "backups", "law")
RUNTIME = os.path.join(ROOT, "configs", "runtime.json")
CHANNEL = os.path.join(DATA, "prompt", "channel.json")  # Step 8：D1 渠道约定（面板可改）
PERSONA_DIR = os.path.join(DATA, "persona", "cards")
ACTIVE = os.path.join(DATA, "persona", "active.json")
LOOP_STOP = os.path.join(ROOT, "logs", "loop.stop")
MAX_AUDIT_DIFF = 20000

# ---- 白名单：键 → 规则 -------------------------------------------------------
SCHEMA = {
    "runtime.quiet_hours": {
        "group": "runtime", "label": "安静时段", "kind": "hours",
        "hint": "这两个小时之间她不主动开口（跨夜写成 23 → 7）",
    },
    "runtime.engine_poll_s": {
        "group": "runtime", "label": "引擎轮询间隔", "kind": "float",
        "min": 0.1, "max": 10.0, "step": 0.1, "unit": "秒",
    },
    "runtime.watchdog_interval_s": {
        "group": "runtime", "label": "看护间隔", "kind": "float",
        "min": 1.0, "max": 120.0, "step": 1.0, "unit": "秒",
    },
    "runtime.desktop_boot_timeout_s": {
        "group": "runtime", "label": "桌面启动等待", "kind": "float",
        "min": 5.0, "max": 300.0, "step": 1.0, "unit": "秒",
    },
    "persona.identity": {
        "group": "persona", "label": "她是谁（identity）", "kind": "text", "max": 1000000,
        "hint": "只进判断面；外貌在资产包 canon.txt 里，不在这里",
    },
    "persona.values": {
        "group": "persona", "label": "在意的东西（values）", "kind": "lines",
        "max_items": 1000000, "max_len": 1000000,   # 2026-10-31 取消上限
    },
    "persona.redlines": {
        "group": "persona", "label": "不许做的事（redlines）", "kind": "lines",
        "max_items": 1000000, "max_len": 1000000,   # 2026-10-31 取消上限
    },
    "persona.overrides": {
        "group": "persona", "label": "特殊情况怎么变（overrides）", "kind": "text", "max": 1000000,
    },
    "persona.rate": {
        "group": "persona", "label": "口癖频率（rate）", "kind": "rate",
        "max_items": 12, "max_per": 50,
    },
    "persona.voice": {
        "group": "persona", "label": "声音包（voice slug）", "kind": "text", "max": 30,
    },
    "persona.looks": {
        "group": "persona", "label": "外观包（looks slug）", "kind": "text", "max": 30,
    },
}

# 2026-10-31（施工 Step 8 · D1 渠道约定）：面板能改的是 render() 真正读到的那几个字段。
# 条数与字数上限已按主人要求取消，所以这里没有 max_msgs / chars_per_msg。
# 2026-10-31：微信只有一栏。群里的额外规矩由 channel.rules(platform, chat_id) 叠加，
# 不在面板上开成独立开关（原来那 7 个"微信群"开关运行时从来没人传，等于假的）。
_CHANNEL_PLATFORMS = (("wechat", "微信"), ("qq", "QQ"))
_CHANNEL_FIELDS = (
    ("blank_line", "要不要空行分段", "bool", "开着 = 允许一段话分几行发"),
    ("allow_voice", "能不能发语音", "bool", "关掉 = 只发字，不发语音"),
    ("allow_markdown", "能不能用 markdown", "bool", "关着就明说不用列表、不用星号"),
    ("mention_required", "公开场合先 @ 对方", "bool", "群里通常是开着的"),
    ("delay_ms", "两条之间隔多久", "int", "0 = 连着发；单位毫秒"),
    ("owner_always_reply", "对着主人要接话", "bool", "他一句话我基本都回"),
    ("ask_hint", "关于提问的提示", "text", "她读到的最后一行"),
)
for _plat, _plat_label in _CHANNEL_PLATFORMS:
    for _name, _label, _kind, _hint in _CHANNEL_FIELDS:
        _rule = {"group": "channel", "platform": _plat, "platform_label": _plat_label,
                 "label": _label, "kind": _kind, "hint": _hint}
        if _kind == "int":
            _rule.update({"min": 0, "max": 60000, "step": 100, "unit": "毫秒"})
        elif _kind == "text":
            _rule["max"] = 1000000   # 2026-10-31 取消上限（原 120 字）
        SCHEMA["channel.%s.%s" % (_plat, _name)] = _rule
del _plat, _plat_label, _name, _label, _kind, _hint, _rule


# ---- 小工具 ------------------------------------------------------------------
from core.infra import egress
from core.infra import skips


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _id() -> str:
    return os.urandom(6).hex()


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _atomic_write(path: str, text: str) -> None:
    """写文本：统一 \n（仓库里的 JSON 都是 LF，别被 Windows 的换行翻译改成 CRLF）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.replace("\r\n", "\n"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _atomic_copy(src: str, dst: str) -> None:
    """逐字节还原（回滚用，保证和备份完全一致）。"""
    tmp = dst + ".tmp"
    with open(src, "rb") as a, open(tmp, "wb") as b:
        b.write(a.read())
        b.flush()
        os.fsync(b.fileno())
    os.replace(tmp, dst)


def _diff(before: str, after: str, name: str) -> str:
    out = difflib.unified_diff(
        before.splitlines(True), after.splitlines(True),
        fromfile="改前 · " + name, tofile="改后 · " + name, n=3,
    )
    return "".join(out)


def active_card_name() -> str:
    try:
        return str(_load(ACTIVE).get("card") or "")
    except Exception:
        return ""


def self_name(default: str = "鲸鲸") -> str:
    """她的**当前名字** = 改名卡（`data/persona/active.json` 指向的卡）里的 `name`。

    界面上凡是「AI 自己」的地方都读这个，别写死「鲸鲸」：换卡即换名。
    注意：`account_registry.role` 里的 `yinyue` 是**枚举值**（改它要迁移表 + 全仓引用），
    它不跟着改名，只跟着改名的是**显示名**。
    """
    try:
        p = persona_path()
        if p:
            nm = str(_load(p).get("name") or "").strip()
            if nm:
                return nm
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(active_card_name() or "").strip() or default
    except Exception:  # noqa: BLE001
        return default


def persona_path() -> str:
    name = active_card_name()
    return os.path.join(PERSONA_DIR, "%s.json" % name) if name else ""


def _target_path(target: str) -> str:
    if target == "runtime":
        return RUNTIME
    if target == "persona":
        return persona_path()
    if target == "channel":
        return CHANNEL
    raise ValueError("不认识的 target：%r" % (target,))


# ---- 校验 --------------------------------------------------------------------
def _check(key: str, value):
    """返回 (ok, 规范化的值 or 错误说明)。"""
    rule = SCHEMA.get(key)
    if rule is None:
        return False, "不在白名单里：%s" % key
    kind = rule["kind"]

    if kind == "hours":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return False, "要两个整数（起、止）"
        try:
            a, b = int(value[0]), int(value[1])
        except Exception:
            return False, "起止都必须是整数"
        if not (0 <= a <= 23 and 0 <= b <= 23):
            return False, "小时要在 0–23 之间"
        return True, [a, b]

    if kind in ("float", "int"):
        try:
            num = float(value)
        except Exception:
            return False, "不是数字"
        if num < rule["min"] or num > rule["max"]:
            return False, "要在 %s–%s 之间" % (rule["min"], rule["max"])
        return True, (int(num) if kind == "int" else round(num, 3))

    if kind == "bool":
        if isinstance(value, bool):
            return True, value
        if isinstance(value, str) and value.strip().lower() in ("true", "1", "on", "yes"):
            return True, True
        if isinstance(value, str) and value.strip().lower() in ("false", "0", "off", "no", ""):
            return True, False
        return False, "只能是开或关"

    if kind == "text":
        text = str(value or "").strip()
        if len(text) > rule["max"]:
            return False, "最多 %d 字（现在是 %d）" % (rule["max"], len(text))
        return True, text

    if kind == "lines":
        if isinstance(value, str):
            items = [x.strip() for x in value.splitlines()]
        else:
            items = [str(x).strip() for x in (value or [])]
        items = [x for x in items if x]
        if len(items) > rule["max_items"]:
            return False, "最多 %d 条" % rule["max_items"]
        for x in items:
            if len(x) > rule["max_len"]:
                return False, "每条最多 %d 字：%s…" % (rule["max_len"], x[:20])
        return True, items

    if kind == "rate":
        items = value or []
        if not isinstance(items, list):
            return False, "rate 要是列表"
        if len(items) > rule["max_items"]:
            return False, "最多 %d 条" % rule["max_items"]
        out = []
        for it in items:
            if isinstance(it, dict):
                word, mx = it.get("word"), it.get("max_per")
            else:
                return False, "每条要是 {word, max_per}"
            word = str(word or "").strip()
            if not word:
                continue
            try:
                mx = int(mx)
            except Exception:
                return False, "「%s」的次数不是整数" % word
            if not (0 <= mx <= rule["max_per"]):
                return False, "「%s」的次数要在 0–%d" % (word, rule["max_per"])
            out.append({"word": word, "max_per": mx})
        return True, out

    return False, "暂不支持的类型：%s" % kind


def _apply_patch(target: str, patch: dict) -> tuple[bool, str, dict]:
    """把 patch 合到目标对象上；返回 (ok, 错误或空串, 新对象)。"""
    path = _target_path(target)
    if not path:
        return False, "找不到要改的文件：%s" % (target,), {}
    if not os.path.exists(path):
        # channel.json 是"只存改过的值"，本来就可以不存在 —— 第一次保存时从空对象起。
        if target != "channel":
            return False, "找不到要改的文件：%s" % (target,), {}
    obj = _load(path) if os.path.exists(path) else {}
    for key, value in (patch or {}).items():
        full = "%s.%s" % (target, key)
        ok, res = _check(full, value)
        if not ok:
            return False, "%s：%s" % (SCHEMA.get(full, {}).get("label", key), res), {}
        obj[key] = res
    return True, "", obj


# ---- 审计 --------------------------------------------------------------------
def _audit_path(when: str | None = None) -> str:
    day = (when or _now())[:10]
    return os.path.join(AUDIT_DIR, "audit-%s.jsonl" % day)


def _audit_row(row: dict) -> None:
    os.makedirs(AUDIT_DIR, exist_ok=True)
    with open(_audit_path(row.get("at")), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def audit_rows(limit: int = 12) -> list[dict]:
    rows: list[dict] = []
    try:
        names = sorted([n for n in os.listdir(AUDIT_DIR) if n.startswith("audit-") and n.endswith(".jsonl")])
    except Exception:
        return rows
    for name in names[-3:]:
        try:
            with open(os.path.join(AUDIT_DIR, name), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except Exception:
            continue
    rows.sort(key=lambda r: r.get("at") or "")
    out = []
    for r in rows[-limit:][::-1]:
        out.append({
            "id": r.get("id"), "at": r.get("at"), "cmd": r.get("cmd"),
            "target": r.get("target"), "summary": r.get("summary") or "",
            "path": r.get("path") or "", "backup": r.get("backup") or "",
            "diff": (r.get("diff") or "")[:4000], "note": r.get("note") or "",
            "undoable": bool(r.get("backup")),
        })
    return out


# ---- 写入 --------------------------------------------------------------------
def _backup(path: str, tag: str) -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = time.strftime("%Y%m%d_%H%M%S") + "_" + tag + ".bak"
    dst = os.path.join(BACKUP_DIR, name)
    if os.path.exists(path):
        with open(path, "rb") as src, open(dst, "wb") as out:
            out.write(src.read())
    else:
        # 文件本来就不存在（例如 channel.json 第一次保存）：备份就是"空"，
        # 回滚会把它写成 {} —— 效果等于回到出厂默认。
        with open(dst, "w", encoding="utf-8", newline="\n") as out:
            out.write("{}\n")
    _prune_backups()
    return dst


def _prune_backups(keep: int = 200) -> None:
    try:
        files = sorted(os.listdir(BACKUP_DIR))
        for name in files[:-keep]:
            os.remove(os.path.join(BACKUP_DIR, name))
    except Exception:
        pass


def _changed_summary(target: str, before: dict, after: dict, patch: dict) -> str:
    bits = []
    for key in patch:
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        bs = json.dumps(b, ensure_ascii=False)
        as_ = json.dumps(a, ensure_ascii=False)
        if len(bs) > 40:
            bs = bs[:40] + "…"
        if len(as_) > 40:
            as_ = as_[:40] + "…"
        bits.append("%s: %s → %s" % (key, bs, as_))
    return "；".join(bits) or "没有实际变化"


def set_values(target: str, patch: dict, *, dry_run: bool = True, by: str = "panel") -> dict:
    ok, err, after = _apply_patch(target, patch)
    if not ok:
        return {"ok": False, "error": err, "dry_run": dry_run}
    path = _target_path(target)
    before = _load(path) if os.path.exists(path) else {}
    before_text, after_text = _dump(before), _dump(after)
    if before_text == after_text:
        return {"ok": True, "dry_run": dry_run, "noop": True, "diff": "",
                "summary": "没有实际变化", "changed": []}
    diff = _diff(before_text, after_text, os.path.basename(path))
    summary = _changed_summary(target, before, after, patch)
    changed = sorted([k for k in patch if before.get(k) != after.get(k)])
    if dry_run:
        return {"ok": True, "dry_run": True, "diff": diff, "summary": summary, "changed": changed}
    backup = _backup(path, target)
    _atomic_write(path, after_text)
    row = {
        "id": _id(), "at": _now(), "by": by, "cmd": "set", "target": target,
        "path": os.path.relpath(path, ROOT), "summary": summary, "changed": changed,
        "before_sha": _sha(before_text), "after_sha": _sha(after_text),
        "backup": os.path.relpath(backup, ROOT), "diff": diff[:MAX_AUDIT_DIFF],
    }
    _audit_row(row)
    return {"ok": True, "dry_run": False, "diff": diff, "summary": summary,
            "changed": changed, "audit_id": row["id"],
            "note": "已写入。%s" % ("引擎下一拍就会读新卡。" if target == "persona" else "运行时参数下一次启动/读到时生效。")}


def rollback(audit_id: str, *, by: str = "panel") -> dict:
    row = None
    for r in audit_rows(limit=80):
        if r.get("id") == audit_id:
            row = r
            break
    if not row or not row.get("backup"):
        return {"ok": False, "error": "找不到这条改动，或它没有备份"}
    bak = os.path.join(ROOT, row["backup"])
    if not os.path.exists(bak):
        return {"ok": False, "error": "备份文件不在了：%s" % row["backup"]}
    target = row.get("target") or ""
    path = _target_path(target)
    with open(path, "rb") as f:
        before_raw = f.read()
    with open(bak, "rb") as f:
        restored_raw = f.read()
    before_text = before_raw.decode("utf-8")
    restored = restored_raw.decode("utf-8")
    _atomic_copy(bak, path)
    new = {
        "id": _id(), "at": _now(), "by": by, "cmd": "rollback", "target": target,
        "path": os.path.relpath(path, ROOT), "summary": "回滚 %s" % audit_id,
        "before_sha": _sha(before_text), "after_sha": _sha(restored),
        "backup": row["backup"], "diff": _diff(before_text, restored, os.path.basename(path))[:MAX_AUDIT_DIFF],
    }
    _audit_row(new)
    return {"ok": True, "dry_run": False, "summary": new["summary"], "audit_id": new["id"],
            "note": "已回滚到 %s 的状态。" % audit_id}


def restart_engine(*, by: str = "panel", wait_s: float = 150.0) -> dict:
    """只重启 AI 引擎（不动调度器/面板）。2026-10-31 改：

    以前它写一个 logs/loop.stop 就回 ok:True，既不确认调度器在不在（调度器死了这张
    纸条永远没人读），也不确认引擎后来起没起；te 更糟的是那张纸条会留下来毒下一次
    「启动全部」（引擎一启动就「收到停止哨兵，退出」）。现在：先查在不在，再写，
    后台等新引擎真的占住端口，失败就把自己写的哨兵删掉。
    """
    import threading

    sched_pid = liveness.read("scheduler").get("pid")
    if not liveness.pid_alive(sched_pid):
        note = "调度器没在跑（pid %s），写 logs/loop.stop 没人读、也不会有人拉起引擎。请用「启动全部」。" % (sched_pid,)
        _svc_note("restart_engine", False, note)
        return {"ok": False, "error": note}
    old_pid = _engine_pid()
    os.makedirs(os.path.dirname(LOOP_STOP), exist_ok=True)
    with open(LOOP_STOP, "w", encoding="utf-8") as f:
        f.write("restart by %s at %s\n" % (by, _now()))
    _audit_row({
        "id": _id(), "at": _now(), "by": by, "cmd": "restart_engine", "target": "",
        "path": "logs/loop.stop", "summary": "请求重启引擎", "backup": "", "diff": "",
    })

    def _verify() -> None:
        t0 = time.time()
        end = t0 + max(2.0, float(wait_s))  # 下限 2 秒：测试会传很小的值
        while time.time() < end:
            new_pid = _engine_pid()
            if new_pid and new_pid != old_pid and _port_open(ENGINE_PORT):
                _svc_note("restart_engine", True,
                          "引擎已换新：pid %s → %s，用了 %.0f 秒" % (old_pid or "-", new_pid, time.time() - t0))
                return
            time.sleep(1.0)
        # 超时：把自己写的毒哨兵撤掉，否则下一次「启动全部」会被它直接叫停
        try:
            if os.path.exists(LOOP_STOP):
                os.remove(LOOP_STOP)
        except OSError as exc:
            from safelog import warn
            warn("restart_engine_sentinel_cleanup_failed", err=repr(exc))
        _svc_note("restart_engine", False,
                  "%.0f 秒内没等到新引擎（旧 pid %s）；已撤掉 logs/loop.stop。看 logs/loop_engine.out"
                  % (time.time() - t0, old_pid or "-"))

    threading.Thread(target=_verify, name="restart-engine-verify", daemon=True).start()
    note = "已写 logs/loop.stop（旧引擎 pid %s）；结果会出现在「上次操作」那盏灯上，约 40–90 秒" % (old_pid or "-",)
    return {"ok": True, "note": note}


# ---- 外貌：让她认得自己（上传 → 读图 → 重写 canon） ---------------------------
LOOKS_ROOT = os.path.join(ROOT, "data", "persona", "assets", "private")
_LOOKS_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
_LOOKS_MAX_BYTES = 12 * 1024 * 1024


def _card_looks() -> str:
    """卡头 front-matter 里的 looks: 字段（决定读哪个 faces 目录）。"""
    card = ""
    try:
        with open(os.path.join(ROOT, "data", "persona", "active.json"), "r", encoding="utf-8") as f:
            card = str(json.load(f).get("card") or "").strip()
    except Exception:  # noqa: BLE001
        card = ""
    for name in ([card] if card else []) + ["鲸鲸"]:
        p = os.path.join(ROOT, "data", "persona", "cards", name + ".md")
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                head = f.read(600)
        except OSError:
            continue
        for line in head.splitlines()[:6]:
            if line.strip().startswith("looks:"):
                v = line.split(":", 1)[1].strip().strip("\"'")
                if v:
                    return v
    return "jingjing"


def _looks_slug(slug: str = "") -> str:
    return (slug or _card_looks() or "jingjing").strip()


def _faces_dir(slug: str = "") -> str:
    return os.path.join(LOOKS_ROOT, _looks_slug(slug), "faces")


def _looks_index(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("images", [])
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"slug": "", "name": "", "version": 1, "updated_at": "", "count": 0, "images": []}


def _looks_save_index(path: str, idx: dict) -> str:
    import shutil as _sh
    idx["count"] = len(idx.get("images") or [])
    idx["updated_at"] = _now()
    backup = ""
    if os.path.exists(path):
        backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            _sh.copy2(path, backup)
        except OSError:
            backup = ""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return backup


def _looks_post(url: str, payload: dict, *, timeout: float = 120.0, headers: dict = None) -> dict:
    rep = egress.post_json(url, payload, lane="local", headers=headers,
                           timeout_ms=duration.to_ms(f'{timeout}s'))
    return json.loads(rep.text)


def _env_get(key: str) -> str:
    v = str(os.environ.get(key) or "").strip()
    if v:
        return v
    try:
        with open(os.path.join(ROOT, ".env"), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if s.startswith(key + "="):
                    return s.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _looks_note(event: str, **kw) -> None:
    """外貌相关的事件记到 logs/looks.log（这个进程没有 structlog，别用 log.*）。"""
    try:
        with open(os.path.join(ROOT, "logs", "looks.log"), "a", encoding="utf-8") as f:
            f.write("%s %s %s\n" % (_now(), event, json.dumps(kw, ensure_ascii=False)))
    except OSError:
        pass


def _looks_jpeg_b64(path: str, *, max_side: int = 512) -> str:
    """压到 512 长边的 JPEG base64 —— 视觉模型看外形够用，上传快、花钱少。"""
    import base64 as _b64
    import io as _io
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = _io.BytesIO()
            im.save(buf, "JPEG", quality=82)
            return _b64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001
        try:
            with open(path, "rb") as f:
                return _b64.b64encode(f.read()).decode("ascii")
        except OSError:
            return ""


_LOOKS_VISION_PROMPT = (
    "只看这个角色的外形：发色发型、眼睛、耳朵或鳍、尾巴、服装、体型、配饰、整体配色。"
    "用 3~5 句中文短句，只写看得见的，不编剧情、不猜性格。"
)


def _looks_describe(path: str, *, max_chars: int = 220) -> str:
    """看一眼这张图，只说外形。先云（deepseek-chat 实测吃图，2 秒），再回本地 ollama。永不抛。"""
    b64 = _looks_jpeg_b64(path)
    if not b64:
        return ""
    key = _env_get("DEEPSEEK_API_KEY")
    if key:
        try:
            out = _looks_post(
                "https://api.deepseek.com/chat/completions",
                {"model": "deepseek-chat", "max_tokens": 320, "temperature": 0.2,
                 "messages": [{"role": "user", "content": [
                     {"type": "text", "text": _LOOKS_VISION_PROMPT},
                     {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]},
                timeout=90, headers={"Authorization": "Bearer " + key},
            )
            txt = str(out["choices"][0]["message"].get("content") or "").strip()
            if txt:
                return txt[:max_chars]
            _looks_note("looks_vision_empty", path=os.path.basename(path))
        except Exception as exc:  # noqa: BLE001
            _looks_note("looks_vision_failed", err=repr(exc)[:160], path=os.path.basename(path))
    try:
        out = _looks_post(
            "http://127.0.0.1:11434/api/chat",
            {"model": "qwen2.5vl:7b", "keep_alive": "30m", "stream": False,
             "messages": [{"role": "user", "content": _LOOKS_VISION_PROMPT, "images": [b64]}],
             "options": {"temperature": 0.2, "num_predict": 200}},
            timeout=300,
        )
        return str((out.get("message") or {}).get("content") or "").strip()[:max_chars]
    except Exception as exc:  # noqa: BLE001
        _looks_note("looks_vision_local_failed", err=repr(exc)[:160])
        return ""


def _looks_text(system: str, user: str, *, timeout: float = 120.0) -> str:
    """写 canon 用的文本模型：deepseek-chat 优先，失败退本地 ollama。"""
    key = _env_get("DEEPSEEK_API_KEY")
    if key:
        try:
            out = _looks_post(
                "https://api.deepseek.com/chat/completions",
                {"model": "deepseek-chat", "temperature": 0.5, "max_tokens": 800,
                 "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": user}]},
                timeout=timeout, headers={"Authorization": "Bearer " + key},
            )
            txt = str(out["choices"][0]["message"].get("content") or "").strip()
            if txt:
                return txt
        except Exception as exc:  # noqa: BLE001
            _looks_note("looks_llm_failed", err=repr(exc)[:160])
    out = _looks_post(
        "http://127.0.0.1:11434/v1/chat/completions",
        {"model": "qwen2.5:7b", "temperature": 0.5, "max_tokens": 800,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": user}]},
        timeout=timeout,
    )
    return str(out["choices"][0]["message"].get("content") or "").strip()


def looks_status(slug: str = "") -> dict:
    fd = _faces_dir(slug)
    ident = os.path.join(fd, "index.json")
    canon_p = os.path.join(fd, "canon.txt")
    idx = _looks_index(ident)
    canon = ""
    try:
        with open(canon_p, "r", encoding="utf-8", errors="replace") as f:
            canon = f.read().strip()
    except OSError:
        canon = ""
    images = []
    for it in idx.get("images") or []:
        f = str(it.get("file") or "")
        p = os.path.join(fd, "gallery", f)
        images.append({
            "file": f,
            "bytes": os.path.getsize(p) if os.path.exists(p) else 0,
            "desc": str(it.get("desc") or ""),
            "note": str(it.get("note") or ""),
            "style": str(it.get("style") or ""),
        })
    return {
        "ok": True,
        "slug": _looks_slug(slug),
        "dir": fd,
        "gallery": os.path.join(fd, "gallery"),
        "count": len(images),
        "canon": canon,
        "canon_chars": len(canon),
        "canon_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(canon_p))) if os.path.exists(canon_p) else "",
        "updated_at": str(idx.get("updated_at") or ""),
        "images": images,
    }


def looks_upload(files, slug: str = "", by: str = "panel") -> dict:
    import base64 as _b64
    fd = _faces_dir(slug)
    gal = os.path.join(fd, "gallery")
    os.makedirs(gal, exist_ok=True)
    ident = os.path.join(fd, "index.json")
    idx = _looks_index(ident)
    if not idx.get("slug"):
        idx["slug"] = _looks_slug(slug)
    have = {str(it.get("md5") or "") for it in idx.get("images") or []}
    added, dup, skipped = [], 0, []
    for item in files or []:
        name = str((item or {}).get("name") or "upload") if isinstance(item, dict) else "upload"
        raw64 = str((item or {}).get("data") or "") if isinstance(item, dict) else ""
        if not raw64:
            skipped.append(name + ":空")
            continue
        if "," in raw64[:120]:
            raw64 = raw64.split(",", 1)[1]
        ext = os.path.splitext(name)[1].lower() or ".jpg"
        if ext not in _LOOKS_EXT:
            skipped.append(name + ":不是图片")
            continue
        try:
            blob = _b64.b64decode(raw64)
        except Exception:  # noqa: BLE001
            skipped.append(name + ":解码失败")
            continue
        if len(blob) > _LOOKS_MAX_BYTES:
            skipped.append(name + ":太大")
            continue
        md5 = __import__("hashlib").md5(blob).hexdigest()
        if md5 in have:
            dup += 1
            continue
        stem = "".join(ch for ch in os.path.splitext(name)[0] if ch.isalnum() or ch in "-_")[:24] or "img"
        fname = time.strftime("%Y%m%d-%H%M%S") + "-" + stem + ext
        with open(os.path.join(gal, fname), "wb") as f:
            f.write(blob)
        w = h = 0
        try:
            from PIL import Image
            with Image.open(os.path.join(gal, fname)) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001
            pass
        idx["images"].append({"file": fname, "md5": md5, "w": w, "h": h,
                              "style": "", "outfit": "", "note": "", "desc": "",
                              "added_at": _now()})
        have.add(md5)
        added.append(fname)
    if not added:
        # 2026-11-01：一张都没收下时不能回 ok:true —— 面板会显示"已上传"，
        # 而实际是空文件 / 不是图片 / 太大 / 全是重复。原因要摆出来。
        reason = ("、".join(skipped[:3]) if skipped
                  else ("全是重复（%d 张）" % dup if dup else "没有收到文件"))
        return {"ok": False, "error": "一张都没收下：" + reason,
                "added": 0, "files": [], "skipped_dup": dup, "skipped": skipped,
                "count": len(idx.get("images") or []), "slug": _looks_slug(slug)}
    backup = _looks_save_index(ident, idx)
    _audit_row({"id": _id(), "at": _now(), "by": by, "cmd": "looks_upload", "target": _looks_slug(slug),
                "path": gal, "summary": "收了 %d 张图（跳过重复 %d）" % (len(added), dup),
                "backup": backup, "diff": ""})
    return {"ok": True, "added": len(added), "files": added, "skipped_dup": dup,
            "skipped": skipped, "count": len(idx.get("images") or []), "slug": _looks_slug(slug)}


LOOKS_SYSTEM = (
    "你是角色形象的记录员。只根据给出的图片描述与备注，写一段第一人称的外形自述，"
    "让角色自己一读就知道\"这是我自己\"。规则：只写外形（发色发型、瞳色、耳朵或鳍、尾巴、"
    "衣服、配饰、体型、配色主题）；不写性格、不写剧情、不写能力、不写名字来历；"
    "描述里没有的不要编；总长 320~400 个汉字；不要分点、不要标题，写成一段连续的话。"
)


def looks_generate(slug: str = "", by: str = "panel", limit: int = 0) -> dict:
    fd = _faces_dir(slug)
    ident = os.path.join(fd, "index.json")
    idx = _looks_index(ident)
    gal = os.path.join(fd, "gallery")
    t0 = time.time()
    described = 0
    for it in idx.get("images") or []:
        if limit and described >= int(limit):
            break
        if str(it.get("desc") or "").strip():
            continue
        p = os.path.join(gal, str(it.get("file") or ""))
        if not os.path.exists(p):
            continue
        d = _looks_describe(p)
        if d:
            it["desc"] = d
            described += 1
    if described:
        _looks_save_index(ident, idx)
    lines = []
    for i, it in enumerate(idx.get("images") or [], 1):
        d = str(it.get("desc") or "").strip()
        note = str(it.get("note") or "").strip()
        outfit = str(it.get("outfit") or "").strip()
        if not (d or note):
            continue
        lines.append("%d. %s%s%s" % (i, d, ("；服装：" + outfit) if outfit else "", ("；备注：" + note) if note else ""))
    if not lines:
        return {"ok": False, "error": "还没有图或还没读出描述，先上传图片", "described": described}
    old = ""
    try:
        with open(os.path.join(fd, "canon.txt"), "r", encoding="utf-8", errors="replace") as f:
            old = f.read().strip()
    except OSError:
        old = ""
    user = "同一个角色的多张图，外形描述如下：\n\n" + "\n".join(lines)
    if old:
        user += "\n\n（旧版本供参考，可修正但别照抄整句）\n" + old
    user += "\n\n请写那段 320~400 字的第一人称外形自述。"
    try:
        canon = _looks_text(LOOKS_SYSTEM, user)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "写不出来：" + repr(exc)[:160], "described": described}
    canon = " ".join(str(canon or "").split())
    if len(canon) > 400:
        cut = canon[:400]
        for sep in ("。", "；", "，"):
            k = cut.rfind(sep)
            if k >= 300:
                cut = cut[:k + 1]
                break
        canon = cut
    if len(canon) < 80:
        return {"ok": False, "error": "写出来的太短（%d 字），没动文件" % len(canon), "described": described}
    canon_p = os.path.join(fd, "canon.txt")
    backup = ""
    if os.path.exists(canon_p):
        backup = canon_p + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            import shutil as _sh
            _sh.copy2(canon_p, backup)
        except OSError:
            backup = ""
    tmp = canon_p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(canon + "\n")
    os.replace(tmp, canon_p)
    ms = int((time.time() - t0) * 1000)
    _audit_row({"id": _id(), "at": _now(), "by": by, "cmd": "looks_generate", "target": _looks_slug(slug),
                "path": canon_p, "summary": "看了 %d 张，重写 canon %d 字" % (described, len(canon)),
                "backup": backup, "diff": ""})
    return {"ok": True, "canon": canon, "canon_chars": len(canon), "described": described,
            "ms": ms, "backup": backup, "slug": _looks_slug(slug)}


def looks_setcanon(text: str = "", slug: str = "", by: str = "panel") -> dict:
    """人肉改她那句自述（不改图，只改文字）。写前备份、超长拒收。"""
    slug = (slug or "").strip() or _looks_slug()
    faces = _faces_dir(slug)
    body = str(text or "").replace("\r\n", "\n").strip()
    if not body:
        return {"ok": False, "error": "内容是空的"}
    if len(body) > 1000000:
        return {"ok": False, "error": "最多 1000000 字（现在是 %d）；引擎只读前 1000000 字，超了会被截断" % len(body)}
    if len(body) < 20:
        return {"ok": False, "error": "太短了（至少 20 字），这不像她的自述"}
    path = os.path.join(faces, "canon.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as _f:
            if _f.read().strip() == body:
                return {"ok": True, "canon": body, "canon_chars": len(body), "note": "跟原来一样，没动"}
    try:
        if os.path.exists(path):
            shutil.copy2(path, os.path.join(faces, "canon.txt.bak-" + time.strftime("%Y%m%d-%H%M%S")))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as _f:
            _f.write(body + "\n")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "写不进去：%r" % (exc,)}
    _audit_row({"at": _now(), "cmd": "looks_setcanon", "by": by,
                "path": str(path), "summary": "人工改自述 %d 字" % len(body)})
    _looks_note("setcanon", by=by, chars=len(body))
    return {"ok": True, "canon": body, "canon_chars": len(body), "path": path}


def looks_delete(file: str = "", slug: str = "", by: str = "panel") -> dict:
    fd = _faces_dir(slug)
    name = os.path.basename(str(file or ""))
    if not name:
        return {"ok": False, "error": "要删哪张？"}
    p = os.path.join(fd, "gallery", name)
    ident = os.path.join(fd, "index.json")
    idx = _looks_index(ident)
    images = idx.get("images") or []
    in_idx = any(str(it.get("file") or "") == name for it in images)
    exists = os.path.isfile(p)
    # 2026-11-01：以前不管文件在不在都写 index、记一条"删掉一张图"、返回 ok:true ——
    # 等于把「什么都没做」报成「做成了」。现在三种情况分开说：
    #   文件还在          → 挪到 .bak-时间戳，清 index
    #   文件没了但 index 有 → 只清孤儿记录（这是合法场景），并在回执里说明
    #   两样都没有        → 直接说没有这张图
    if not exists and not in_idx:
        return {"ok": False, "error": "没有这张图：%s" % name}
    idx["images"] = [it for it in images if str(it.get("file") or "") != name]
    _looks_save_index(ident, idx)
    moved = ""
    if exists:
        moved = p + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        try:
            os.replace(p, moved)
        except OSError:
            moved = ""
    _audit_row({"id": _id(), "at": _now(), "by": by, "cmd": "looks_delete", "target": _looks_slug(slug),
                "path": p, "summary": "删掉一张图", "backup": moved, "diff": ""})
    return {"ok": True, "deleted": name, "count": len(idx.get("images") or []),
            "file_missing": not exists}


# ---- 说话白名单（平台 → 账号 → 联系人/群） ----------------------------------
def _sp_platforms() -> list:
    base = os.path.join(ROOT, "data", "platforms")
    out = []
    if not os.path.isdir(base):
        return out
    for plat in sorted(os.listdir(base)):
        pdir = os.path.join(base, plat)
        if not os.path.isdir(pdir):
            continue
        accs = [a for a in sorted(os.listdir(pdir)) if os.path.isdir(os.path.join(pdir, a))]
        out.append({"key": plat, "label": {"wechat": "微信", "qq": "QQ"}.get(plat, plat), "accounts": accs})
    return out


def _sp_rows(platform: str, account_id: str, days: int = 30) -> list:
    """这个账号下近 N 天有消息的会话（含名字）。读不到就回空表。"""
    import sqlite3

    p = os.path.join(ROOT, "data", "platforms", platform, account_id, "account.db")
    if not os.path.isfile(p):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        names = {}
        try:
            for r in con.execute("select wxid, nickname, remark from contacts"):
                nm = str(r["remark"] or "").strip() or str(r["nickname"] or "").strip()
                if nm:
                    names[str(r["wxid"])] = nm
        except Exception:  # noqa: BLE001
            pass
        rows = []
        try:
            for r in con.execute(
                    "select chat_id, count(*) n, max(created_at) last_at from messages"
                    " where created_at >= datetime('now', ?) group by chat_id"
                    " order by last_at desc", ("-%d day" % int(days),)):
                cid = str(r["chat_id"] or "")
                if not cid:
                    continue
                rows.append({"chat_id": cid, "name": names.get(cid, ""),
                             "n": int(r["n"] or 0), "last_at": str(r["last_at"] or ""),
                             "is_group": cid.endswith("@chatroom")})
        except Exception:  # noqa: BLE001
            pass
        con.close()
        return rows
    except Exception:  # noqa: BLE001
        return []


def _sp_contacts(platform: str, account_id: str, chatted: set = None, limit: int = 0) -> list:
    """通讯录里"还没聊过"的联系人/群（这两类原来根本不进候选）。

    2026-10-28：主人要"新加的好友自动出现在白名单勾选框里"。
    只从 messages 取会话时，刚加、还没说话的人看不到。
    注意：contacts.updated_at 每次同步都会被刷新，用它判"新加"是错的（一测 299/300 全标新）；
    可靠信号是"这个 chat_id 从来没出现在 messages 里"→ 标「未聊过」。
    QQ 的 contacts 把群成员也当联系人灌进来了（1 万+），所以 QQ 只补 is_group=1 的群。
    """
    import sqlite3

    chatted = chatted or set()
    p = os.path.join(ROOT, "data", "platforms", platform, account_id, "account.db")
    if not os.path.isfile(p):
        return []
    out = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        for r in con.execute("select wxid, nickname, remark, is_group from contacts"):
            cid = str(r["wxid"] or "").strip()
            if not cid or cid in chatted:
                continue
            is_grp = 1 if r["is_group"] else 0
            if platform == "qq" and not is_grp:
                continue
            nm = str(r["remark"] or "").strip() or str(r["nickname"] or "").strip()
            if not nm:
                continue
            out.append({"chat_id": cid, "name": nm + "（未聊过）", "n": 0, "last_at": "",
                        "is_group": is_grp, "is_new": True, "src": "contacts"})
            if len(out) >= int(limit):
                break
        con.close()
    except Exception:  # noqa: BLE001
        return out
    return out


def _sp_rows_all(platform: str, account_id: str, days: int = 30) -> list:
    """近 N 天有消息的会话 ∪ 通讯录里还没聊过的（标「未聊过」，排在最前）。"""
    import sqlite3

    base = {}
    for r in _sp_rows(platform, account_id, days):
        base[str(r.get("chat_id") or "")] = dict(r, src="messages", is_new=False)
    chatted = set(base.keys())
    # 补上"聊过但超出 N 天"的，避免把老朋友误标成未聊过
    try:
        p = os.path.join(ROOT, "data", "platforms", platform, account_id, "account.db")
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)
        for r in con.execute("select distinct chat_id from messages"):
            chatted.add(str(r[0] or ""))
        con.close()
    except Exception:  # noqa: BLE001
        pass
    for r in _sp_contacts(platform, account_id, chatted):
        base.setdefault(r["chat_id"], r)
    rows = [v for k, v in base.items() if k]
    rows.sort(key=lambda x: (0 if x.get("is_new") else 1, str(x.get("last_at") or "")))
    return rows


def speak_policy_get(days: int = 30) -> dict:
    """面板用：平台 → 账号 → 会话，带"现在允不允许说话"。"""
    from loop import speak_policy as sp

    data = sp.load()
    try:
        _mtime = float(sp.PATH.stat().st_mtime)
    except OSError:
        _mtime = 0.0
    out = {"ok": True, "enabled": sp.is_enabled(), "updated_at": str(data.get("updated_at") or ""),
           "mtime": _mtime,
           "path": str(sp.PATH), "platforms": []}
    for plat in _sp_platforms():
        accs = []
        for aid in plat["accounts"]:
            box = ((data.get("platforms") or {}).get(plat["key"], {}) or {}).get("accounts", {}) or {}
            mine = box.get(aid) or {}
            mode = str(mine.get("mode") or "allowlist")
            allowed = (mine.get("chats") or {})
            rows = _sp_rows_all(plat["key"], aid, days)
            chats = []
            for r in rows:
                cid = r["chat_id"]
                rec = allowed.get(cid) or {}
                chats.append({"is_new": bool(r.get("is_new")), "src": r.get("src", ""),
                          "chat_id": cid, "name": r["name"], "n": r["n"], "last_at": r["last_at"],
                              "is_group": r["is_group"],
                              "allowed": True if mode == "all" else (cid in allowed),
                              "proactive": bool((rec or {}).get("proactive"))})
            chats.sort(key=lambda x: (-int(x["n"]), x["chat_id"]))
            accs.append({"account_id": aid, "mode": mode, "chats": chats,
                         "n_chats": len(chats), "n_allowed": sum(1 for c in chats if c["allowed"]),
                         "n_proactive": sum(1 for c in chats if c.get("proactive")),
                         "configured": bool(mine)})
        out["platforms"].append({"key": plat["key"], "label": plat["label"], "accounts": accs})
    return out


def speak_policy_proactive(accounts=None, by: str = "panel") -> dict:
    """只改「可主动」标记；不改谁在白名单里。accounts=[{platform, account_id, proactive:[chat_id...]}]"""
    from loop import speak_policy as sp

    if not isinstance(accounts, list):
        return {"ok": False, "error": "accounts 要是数组"}
    data = sp.load()
    data.setdefault("version", 1)
    plats = data.setdefault("platforms", {})
    n_on = n_off = 0
    for ac in accounts:
        if not isinstance(ac, dict):
            continue
        plat = str(ac.get("platform") or "")
        aid = str(ac.get("account_id") or "")
        try:
            want = set(str(x) for x in (ac.get("proactive") or []))
        except Exception:
            want = set()
        box = plats.setdefault(plat, {"default": "allowlist", "accounts": {}})
        node = (box.setdefault("accounts", {})).setdefault(aid, {"mode": "allowlist", "chats": {}})
        chats = node.setdefault("chats", {})
        for cid in list(chats.keys()):
            ent = chats[cid] if isinstance(chats[cid], dict) else {}
            flag = cid in want
            if bool(ent.get("proactive")) != flag:
                ent["proactive"] = flag
                chats[cid] = ent
                if flag:
                    n_on += 1
                else:
                    n_off += 1
    sp.save(data)
    _audit_row({"at": _now(), "kind": "cmd", "path": "data/persona/speak_policy.json",
                "summary": "可主动标记：+%d / -%d" % (n_on, n_off), "by": by, "ok": True})
    tg = sp.proactive_targets()
    return {"ok": True, "on": n_on, "off": n_off,
            "targets": [t.get("name") or t.get("chat_id") for t in tg],
            "note": "可主动开口的地方现在有 %d 个" % len(tg)}

def speak_policy_search(q: str = "", limit: int = 20) -> dict:
    """按名字 / chat_id 搜**任意**会话（不只近 30 天聊过的），给「一键加进来」用。

    2026-11-05 §43.6 第二条：面板原来的搜索框只在 `speak_policy_get` 已经装进内存的
    那份列表里过滤（近 30 天有消息的 ∪ 通讯录），**超出 30 天又不在通讯录的群搜不到**。
    这里直接翻 account.db：通讯录全量 + `messages` 里出现过的 chat_id 全量。

    两处细节（第一版踩过）：
    1. 同一个会话可能出现在多个账号的通讯录里 —— 按 (平台, chat_id) 合并，`n`/`last_at`
       取有消息的那份，名字优先用人给的名字；
    2. 「已在名单里」按**整个平台**算：名单是按账号存的，但主人看到的是「这个群她能不能说话」。
    """
    import sqlite3

    from loop import speak_policy as sp

    key = str(q or "").strip().lower()
    if not key:
        return {"ok": False, "error": "给个名字或 chat_id", "items": []}
    data = sp.load()
    found: dict = {}
    errs: list[str] = []
    for plat in _sp_platforms():
        pkey = str(plat.get("key") or "")
        box = ((data.get("platforms") or {}).get(pkey, {}) or {}).get("accounts", {}) or {}
        for aid in (plat.get("accounts") or []):
            path = os.path.join(ROOT, "data", "platforms", pkey, aid, "account.db")
            if not os.path.isfile(path):
                continue
            cand: list[dict] = []
            try:
                con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
                con.row_factory = sqlite3.Row
                for r in con.execute("select wxid, nickname, remark from contacts"):
                    cid = str(r["wxid"] or "").strip()
                    if not cid:
                        continue
                    nm = str(r["remark"] or "").strip() or str(r["nickname"] or "").strip()
                    cand.append({"chat_id": cid, "name": nm, "src": "contacts",
                                 "n": 0, "last_at": ""})
                for r in con.execute("select chat_id, count(*) as n, max(created_at) as last_at "
                                     "from messages group by chat_id"):
                    cid = str(r["chat_id"] or "").strip()
                    if not cid:
                        continue
                    cand.append({"chat_id": cid, "name": "", "src": "messages",
                                 "n": int(r["n"] or 0), "last_at": str(r["last_at"] or "")})
                con.close()
            except Exception as exc:  # noqa: BLE001 - 一个账号的库读不了就跳过，原因带回去
                errs.append("%s/%s: %s" % (pkey, aid, type(exc).__name__))
                continue
            for c in cand:
                cur = found.get((pkey, c["chat_id"]))
                if cur is None:
                    found[(pkey, c["chat_id"])] = {
                        "chat_id": c["chat_id"], "platform": pkey, "account_id": aid,
                        "name": str(c.get("name") or ""), "n": int(c.get("n") or 0),
                        "last_at": str(c.get("last_at") or ""),
                        "is_group": 1 if "@chatroom" in c["chat_id"] else 0,
                        "accounts": [aid], "allowed": False, "proactive": False}
                    continue
                if c.get("n"):
                    cur["n"] = int(c["n"])
                if c.get("last_at") and str(c["last_at"]) > str(cur.get("last_at") or ""):
                    cur["last_at"] = str(c["last_at"])
                if c.get("name") and (not cur["name"] or cur["name"] == cur["chat_id"]):
                    cur["name"] = str(c["name"])
                if aid not in cur["accounts"]:
                    cur["accounts"].append(aid)
    # 「已在名单里」按整个平台算
    for (pkey, cid), row in found.items():
        accs = ((data.get("platforms") or {}).get(pkey, {}) or {}).get("accounts", {}) or {}
        for aid, mine in accs.items():
            ent = ((mine or {}).get("chats") or {}).get(cid) or {}
            if ent:
                row["allowed"] = True
                row["account_id"] = aid
                if ent.get("name"):
                    row["name"] = str(ent["name"])
                if ent.get("proactive"):
                    row["proactive"] = True
                break
    out = []
    for row in found.values():
        if key not in str(row.get("name") or "").lower() and key not in row["chat_id"].lower():
            continue
        out.append(row)
    out.sort(key=lambda x: (0 if x["allowed"] else 1, 0 if str(x.get("last_at") or "") else 1,
                            str(x.get("last_at") or ""), str(x.get("name") or "")))
    got = {"ok": True, "q": str(q or ""), "items": out[:int(limit or 20)], "total": len(out)}
    if errs:
        got["errors"] = errs
    return got


def speak_policy_seed(days: int = 30, by: str = "panel") -> dict:
    """把近 N 天有往来的会话全部加进白名单（等于"保持现状"，之后你再一个个关掉）。"""
    from loop import speak_policy as sp

    data = sp.load()
    data.setdefault("version", 1)
    plats = data.setdefault("platforms", {})
    added = 0
    for plat in _sp_platforms():
        box = plats.setdefault(plat["key"], {"default": "allowlist", "accounts": {}})
        box.setdefault("accounts", {})
        for aid in plat["accounts"]:
            mine = box["accounts"].setdefault(aid, {"mode": "allowlist", "chats": {}})
            mine.setdefault("chats", {})
            mine["mode"] = str(mine.get("mode") or "allowlist")
            for r in _sp_rows(plat["key"], aid, days):
                cid = r["chat_id"]
                if cid in mine["chats"]:
                    if r["name"] and (mine["chats"][cid] or {}).get("name") != r["name"]:
                        mine["chats"][cid]["name"] = r["name"]
                    continue
                mine["chats"][cid] = {"name": r["name"], "at": _now(), "by": by, "seed": True}
                added += 1
    sp.save(data)
    _audit_row({"at": _now(), "kind": "cmd", "path": "data/persona/speak_policy.json",
                "summary": "初始化说话白名单，新增 %d 个会话" % added, "by": by, "ok": True})
    return {"ok": True, "added": added, "note": "已把近 %d 天有往来的会话加入白名单（新增 %d 个）" % (days, added)}


def speak_policy_set(accounts: list | None = None, by: str = "panel",
                     expect_mtime: float = 0.0) -> dict:
    """整表写回。accounts = [{platform, account_id, mode, allowed:[chat_id,...]}]"""
    from loop import speak_policy as sp

    items = accounts if isinstance(accounts, list) else []
    if not items:
        return {"ok": False, "error": "没收到要保存的内容"}
    old = sp.load()
    # 2026-11-05 §43.6（顺手补的一个真 bug）：面板是**整表写回** —— 页面拿一份 mtime，
    # 保存时对不上就说明这中间名单被别处改过（一键加白名单 / 另一个窗口 / 手改文件），
    # 硬写会把人家刚加的那条**悄悄抹掉**（示例作业群就是这么丢过一次）。
    if expect_mtime:
        try:
            _mt = float(sp.PATH.stat().st_mtime)
        except OSError:
            _mt = 0.0
        if _mt and abs(_mt - float(expect_mtime)) > 0.001:
            return {"ok": False, "stale": True,
                    "error": ("名单在你打开面板之后被改过（文件 %s）—— 点一下刷新再保存，"
                              "别把刚加的那条抹掉" % time.strftime("%H:%M:%S", time.localtime(_mt)))}
    data = {"version": 1, "platforms": {}}
    kept = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        plat = str(it.get("platform") or "").strip()
        aid = str(it.get("account_id") or "").strip()
        if not plat or not aid:
            continue
        mode = str(it.get("mode") or "allowlist").lower()
        if mode not in ("allowlist", "all", "none"):
            mode = "allowlist"
        allow = [str(x) for x in (it.get("allowed") or []) if str(x).strip()]
        names = {}
        try:
            for r in _sp_rows(plat, aid, 3650):
                names[r["chat_id"]] = r["name"]
        except Exception:  # noqa: BLE001
            pass
        box = data["platforms"].setdefault(plat, {"default": "allowlist", "accounts": {}})
        chats = {}
        for cid in allow:
            prev = (((old.get("platforms") or {}).get(plat, {}) or {}).get("accounts", {}) or {}).get(aid, {}) \
                .get("chats", {}).get(cid) or {}
            _ent = {"name": names.get(cid) or str(prev.get("name") or ""),
                    "at": _now(), "by": by}
            if prev.get("proactive"):   # 2026-10-27 修：保存白名单时不要把"可主动"洗掉
                _ent["proactive"] = True
            chats[cid] = _ent
            kept += 1
        box["accounts"][aid] = {"mode": mode, "chats": chats}
    try:
        bak = sp.PATH.with_suffix(".json.bak-" + time.strftime("%Y%m%d-%H%M%S"))
        if sp.PATH.exists():
            bak.write_text(sp.PATH.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    sp.save(data)
    _audit_row({"at": _now(), "kind": "cmd", "path": "data/persona/speak_policy.json",
                "summary": "保存说话白名单，共 %d 个会话" % kept, "by": by, "ok": True})
    return {"ok": True, "n": kept, "note": "已保存：%d 个会话可说话" % kept}


# ---- 给面板看的现状 ----------------------------------------------------------
def values() -> dict:
    out = {"runtime": {}, "persona": {}, "card": active_card_name()}
    try:
        rt = _load(RUNTIME)
        for key in SCHEMA:
            if key.startswith("runtime."):
                out["runtime"][key.split(".", 1)[1]] = rt.get(key.split(".", 1)[1])
    except Exception as exc:
        out["runtime_error"] = repr(exc)
    try:
        card = _load(persona_path())
        for key in SCHEMA:
            if key.startswith("persona."):
                out["persona"][key.split(".", 1)[1]] = card.get(key.split(".", 1)[1])
    except Exception as exc:
        out["persona_error"] = repr(exc)
    try:
        from core.policy import channel as _ch

        out["channel"] = {"%s.%s" % (plat, key): val
                          for plat, _lbl in _CHANNEL_PLATFORMS
                          for key, val in _ch.effective(plat).items()}
    except Exception as exc:  # noqa: BLE001
        out["channel"] = {}
        out["channel_error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        out["cards"] = card_list().get("cards") or []
    except Exception as exc:  # noqa: BLE001
        out["cards"] = []
        out["cards_error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        out["voice"] = voice_info()
    except Exception as exc:  # noqa: BLE001
        out["voice"] = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return out


def schema() -> list[dict]:
    rows = []
    for key, rule in SCHEMA.items():
        row = dict(rule)
        row["key"] = key
        rows.append(row)
    return rows


AUDIT_STATE_LIMIT = 80   # 2026-10-31：面板只能回滚看到的那几条 —— 12 条太小，
                         # 第 13 次改动就再也点不到「回滚」了（rollback 自己找得着，界面找不着）。
                         # 80 条 ≈ 最近两三天的全部改动，够用且不至于把 /law 撑大。


def state() -> dict:
    # 2026-10-27：人设只有一个字段 —— 面板拿到的是整张卡的正文（text/chars/cap），
    # 不是一堆分组字段。别的房间（runtime 等）照旧。
    rows = audit_rows(AUDIT_STATE_LIMIT)
    return {"ok": True, "schema": schema(), "values": values(), "audit": rows,
            "audit_cap": AUDIT_STATE_LIMIT,
            "card": card_show(active_card_name() or ""), "me_names": me_names_get(),
            "now": _now()}


# ── 主机与账号（2026-10-27）：主程序 / AI 引擎的一键启停 + 平台账号只读盘点 ──
# 机制一条新都不造：启动 = 删 all.stop/watchdog.stop 后 DETACHED 拉
# scripts/supervisor.py（与 鲸鲸.bat 第 1 步一致）；停止 = 写 logs/all.stop
# 并替用户按下 scheduler.stop / loop.stop（与 scripts/supervisor.py 的
# _press_sentinels 一致）；重启 = 停 → 等 39871 释放 → 起（与
# scripts/restart_service.py、重启鲸鲸.bat 一致）。
LOGS_DIR = os.path.join(ROOT, "logs")
ALL_STOP = os.path.join(LOGS_DIR, "all.stop")
WATCHDOG_STOP = os.path.join(LOGS_DIR, "watchdog.stop")
SCHED_STOP = os.path.join(LOGS_DIR, "scheduler.stop")
PYW = os.path.join(ROOT, ".venv_iso", "Scripts", "pythonw.exe")
SUPERVISOR = os.path.join(ROOT, "scripts", "supervisor.py")
ENGINE_PORT = 39871
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NO_WINDOW


def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 —— 文件不在/写一半都当没有
        return {} if default is None else default


def _port_open(port: int, timeout: float = 0.5) -> bool:
    import socket

    try:
        with egress.connect("127.0.0.1", port, timeout_ms=duration.to_ms(f'{timeout}s')):
            return True
    except OSError:
        return False


def _age(path: str):
    try:
        return round(time.time() - os.path.getmtime(path), 1)
    except OSError:
        return None


DESK_LOCK = os.path.join(LOGS_DIR, "desk.lock")
SUPERVISOR_LOCK = os.path.join(LOGS_DIR, "supervisor.lock")
SVC_LAST = os.path.join(LOGS_DIR, "svc_last.json")


def _all_stops() -> tuple:
    """四个哨兵：总闸 / 外部看门狗 / 主程序自停 / 引擎自停。

    2026-10-31：以前「启动全部」只清 all.stop + watchdog.stop，scheduler.stop 与
    loop.stop 只能靠 supervisor 自己启动时顺手清；而 supervisor 有两个提前退出点
    （见 all.stop 就退 / _take_over 不通过就退），一退就没人清了。于是「停止全部 →
    重启引擎 → 启动全部」会留下一个毒 loop.stop：引擎一启动就打印「收到停止哨兵，
    退出」，表现成「点了启动还是起不来」。四个必须一起清。
    """
    return ALL_STOP, WATCHDOG_STOP, SCHED_STOP, LOOP_STOP


def _clear_stops() -> tuple:
    """删掉所有停止哨兵，返回 (删掉的相对路径, 删不掉的说明)。"""
    removed, errors = [], []
    for path in _all_stops():
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
            removed.append(os.path.relpath(path, ROOT))
        except OSError as exc:
            errors.append("%s（%s）" % (os.path.relpath(path, ROOT), exc))
    return removed, errors


def _read_pid_file(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            return int((f.read() or "").strip() or 0)
    except (OSError, ValueError):
        return 0


def _supervisor_pid() -> int:
    """监管器自己写的锁（scripts/supervisor.py:SUPERVISOR_LOCK）。"""
    pid = _read_pid_file(SUPERVISOR_LOCK)
    return pid if liveness.pid_alive(pid) else 0


def _desk_pid() -> int:
    pid = _read_pid_file(DESK_LOCK)
    return pid if liveness.pid_alive(pid) else 0


def _engine_pid() -> int:
    try:
        pid = int(liveness.read("engine").get("pid"))
    except (TypeError, ValueError):
        return 0
    return pid if liveness.pid_alive(pid) else 0


def _svc_note(cmd: str, ok: bool, note: str) -> None:
    """长操作的最终结果落一行，给下一轮 svc_status 显示（面板不阻塞地看结果）。"""
    os.makedirs(LOGS_DIR, exist_ok=True)
    if not atomic_write_json(SVC_LAST, {"cmd": cmd, "ok": bool(ok), "note": note, "at": _now()}):
        # 写不下就只在日志里说一声：面板少一盏「上次操作」灯，不该因此让按钮失败
        from safelog import warn
        warn("svc_note_write_failed", cmd=cmd, err="atomic_write_failed")


def _svc_last() -> dict:
    return _read_json(SVC_LAST, {})


def svc_status() -> dict:
    hb = liveness.read("scheduler")
    eng = liveness.read("engine")
    main_pid, eng_pid = hb.get("pid"), eng.get("pid")
    main_age = liveness.age_ms("scheduler")
    eng_age = liveness.age_ms("engine")
    port = _port_open(ENGINE_PORT)
    desk = _desk_pid()
    lights = [
        {"key": "main", "name": "主程序（调度器）", "pid": main_pid,
         "alive": liveness.pid_alive(main_pid), "age_ms": main_age,
         "age_text": duration.human(main_age) if main_age is not None else "无心跳",
         "extra": "心跳 %s 拍" % hb.get("main_loop_ticks") if hb.get("main_loop_ticks") is not None else "",
         "hint": "日志 logs/yinyue.log"},
        # 2026-10-31：引擎灯的判据从「心跳文件新不新」改成「端口通不通」。
        # 心跳是引擎自己的 keepalive 线程刷的，卡住时照样新鲜（今天下午空转两小时
        # 就是这么躲过监管器的）；端口是内核级硬判据，scheduler 也是这么判的。
        {"key": "engine", "name": "AI 引擎（loop）", "pid": eng_pid,
         "alive": bool(port and liveness.pid_alive(eng_pid)), "age_ms": eng_age,
         "age_text": duration.human(eng_age) if eng_age is not None else "无心跳",
         "extra": "tick %s · 上次 %s · 端口 %s" % (
             eng.get("ticks"), eng.get("last_tick_at"), "通" if port else "不通"),
         "hint": "日志 logs/loop_engine.out"},
        {"key": "desk", "name": "发送工（desk）", "pid": desk or "-",
         "alive": bool(desk), "age_ms": 0,
         "age_text": "在跑" if desk else "不在，话会堆在队列里",
         "extra": "logs/desk.lock", "hint": "它不在时她说的话发不出去（2026-10-30 那次堆了 13 小时）"},
        {"key": "panel", "name": "面板采集器", "pid": os.getpid(), "alive": True,
         "age_ms": 0, "age_text": "刚刚",
         "extra": "127.0.0.1:%d" % 39872, "hint": "窗口关掉它就退"},
    ]
    last = _svc_last()
    if last:
        lights.append({"key": "last", "name": "上次操作 · %s" % (last.get("cmd") or "?"),
                       "pid": "-", "alive": bool(last.get("ok")), "age_ms": 0,
                       "age_text": str(last.get("at") or ""),
                       "extra": str(last.get("note") or "")[:90], "hint": "logs/svc_last.json"})
    sentinels = [os.path.basename(p) for p in _all_stops() if os.path.exists(p)]
    return {"ok": True, "lights": lights, "port_open": port,
            "sentinels": sentinels, "all_stop": os.path.exists(ALL_STOP),
            "watchdog_stop": os.path.exists(WATCHDOG_STOP),
            "supervisor_pid": _supervisor_pid(), "desk_pid": desk, "now": _now()}


def svc_start(*, by: str = "panel") -> dict:
    import subprocess

    removed, errors = _clear_stops()
    if errors:
        note = "删不掉哨兵：%s —— 可能是权限问题" % "、".join(errors)
        _svc_note("svc_start", False, note)
        return {"ok": False, "error": note}
    sup = _supervisor_pid()
    sched_alive = liveness.pid_alive(liveness.read("scheduler").get("pid"))
    port = _port_open(ENGINE_PORT)
    # 2026-10-31：判据以前是 `and not removed` —— 只要清掉过任何一个哨兵就无条件拉一次
    # 监管器（哪怕它已经在跑）。现在只问「监管器活着吗」，清哨兵归清哨兵。
    if sup and sched_alive and port:
        note = "已经在跑（监管器 pid %d、主程序在、端口 %d 通），没有重复启动%s" % (
            sup, ENGINE_PORT, "；清掉了 %s" % "、".join(removed) if removed else "")
        _svc_note("svc_start", True, note)
        return {"ok": True, "noop": True, "note": note, "removed": removed}
    for path, label in ((SUPERVISOR, "监管器"), (PYW, "解释器")):
        if not os.path.exists(path):
            note = "找不到%s %s" % (label, os.path.relpath(path, ROOT))
            _svc_note("svc_start", False, note)
            return {"ok": False, "error": note}
    proc = subprocess.Popen([PYW, SUPERVISOR], cwd=ROOT, creationflags=_DETACHED,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True)
    row = {"id": _id(), "at": _now(), "by": by, "cmd": "svc_start", "target": "",
           "path": os.path.relpath(SUPERVISOR, ROOT), "summary": "启动全部",
           "backup": "", "diff": "", "note": "清掉 %s" % ("、".join(removed) or "无残留哨兵")}
    _audit_row(row)
    note = "已拉起监管器 pid %d（清掉 %s）；10~20 秒内主程序和引擎会自己起来" % (
        proc.pid, "、".join(removed) or "无残留哨兵")
    _col = _ensure_collector()
    if _col.get("started"):
        note += "；面板后端也顺手拉起来了（pid %s）" % _col.get("pid")
    elif not _col.get("ok"):
        note += "；但面板后端没起来（%s）" % (_col.get("error") or "未知")
    _svc_note("svc_start", True, note)
    return {"ok": True, "audit_id": row["id"], "note": note,
            "removed": removed, "supervisor_pid": proc.pid, "collector": _col}


COLLECTOR_PORT = 39872


def _ensure_collector(timeout_s: float = 6.0) -> dict:
    """确保面板后端（采集器）在跑。

    2026-11-01：`svc_start` / `svc_restart` 原来只拉 supervisor，**不管采集器** ——
    采集器跟着面板窗口活（窗口没了它自己退），所以重启服务之后页面还在、数据却断了，
    面板顶栏就红成「连不上采集器」。这里补上：39872 不通就 detached 起一个。
    """
    if _port_open(COLLECTOR_PORT):
        return {"ok": True, "started": False, "pid": 0}
    pyw = PYW
    if not os.path.isfile(pyw):
        return {"ok": False, "started": False, "error": "找不到解释器：%s" % pyw}
    script = os.path.join(ROOT, "console", "collector.py")
    if not os.path.isfile(script):
        return {"ok": False, "started": False, "error": "找不到采集器：%s" % script}
    err_path = os.path.join(DATA, "console", "collector.err")
    pid = 0
    try:
        os.makedirs(os.path.dirname(err_path), exist_ok=True)
        with open(err_path, "ab") as err:
            proc = subprocess.Popen([pyw, script], cwd=ROOT, creationflags=_DETACHED,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=err, close_fds=True)
            pid = int(getattr(proc, "pid", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "started": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    deadline = time.time() + max(1.0, float(timeout_s))
    while time.time() < deadline:
        if _port_open(COLLECTOR_PORT):
            return {"ok": True, "started": True, "pid": pid}
        time.sleep(0.3)
    return {"ok": False, "started": True, "pid": pid,
            "error": "采集器起来了（pid %d）但 %d 没通，看 data/console/collector.err" % (pid, COLLECTOR_PORT)}

# ---------------------------------------------------------------- 表情包（只读）
# 2026-11-01：面板要"从她发过的表情里挑"，不是"从硬盘上传"。
# 数据源是世界库自己发出的 sticker 行（is_self=1），正文里带 [表情包:<md5>]；
# 图片去 data/emoji_cache/<md5>.dat（原图）或 data/image_cache/<md5>.png 找。
# 是否已登记：拿 registry 里 templates/<id>.* 的文件内容 md5 与池子里的 md5 对上。
_STICKER_RE = re.compile(r"\[表情包:([0-9a-f]{16,})\]")


def _thumb_png(path: str, size: int = 96) -> bytes:
    """把图缩成一张 PNG（GIF 取首帧）。失败给 b""。"""
    try:
        import io

        from PIL import Image
        with Image.open(path) as im:
            if getattr(im, "is_animated", False):
                im.seek(0)
            pic = im.convert("RGBA")
            pic.thumbnail((size, size))
            buf = io.BytesIO()
            pic.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        from safelog import warn
        warn("sticker_thumb_failed", err=repr(exc), path=path[-60:])
        return b""


def _sticker_thumb(path: str, size: int = 96) -> str:
    """缩成一格缩略图，返回 data:image/png;base64,…；失败给空串（只给小批量用）。"""
    png = _thumb_png(path, size=size)
    if not png:
        return ""
    import base64
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


_THUMB_CACHE = os.path.join(CONSOLE, "thumbs")


def sticker_thumb_bytes(md5: str, size: int = 96) -> bytes:
    """面板 /thumb/<md5> 用：按需生成一张缩略图，落盘缓存（同一个 md5 只解码一次）。

    2026-11-01：sticker_pool 原先把 887 张缩略图内联成 base64 一次吐给前端
    （15.4 MB / 9.3 秒），前端 25 秒超时后只显示「读不到」。改成「一次只给地址，
    浏览器按页来取」，首屏只解码当前页的 60 张。
    """
    md5 = str(md5 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{16,64}", md5):
        return b""
    cache = os.path.join(_THUMB_CACHE, md5 + ".png")
    try:
        with open(cache, "rb") as fh:
            got = fh.read()
        if got:
            return got
    except OSError as exc:
        skips.skip("executor.sticker_thumb_bytes:cache_read", "absent",
                   "缩略图缓存还没生成：%r" % (exc,))
    src = ""
    for ext in (".dat", ".png", ".gif", ".webp", ".jpg", ".jpeg"):
        for d in (os.path.join(DATA, "emoji_cache"), os.path.join(DATA, "image_cache")):
            cand = os.path.join(d, md5 + ext)
            if os.path.isfile(cand):
                src = cand
                break
        if src:
            break
    if not src:
        return b""
    png = _thumb_png(src, size=size)
    if png:
        try:
            os.makedirs(_THUMB_CACHE, exist_ok=True)
            tmp = cache + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(png)
            os.replace(tmp, cache)
        except OSError as exc:
            skips.skip("executor.sticker_thumb_bytes:cache_write", "degraded",
                       "缩略图写不进缓存（下次现解码）：%r" % (exc,))
    return png


def _sticker_image(md5: str) -> str:
    for cand in (os.path.join(DATA, "emoji_cache", md5 + ".dat"),
                 os.path.join(DATA, "image_cache", md5 + ".png"),
                 os.path.join(DATA, "image_cache", md5 + ".dat")):
        if os.path.isfile(cand):
            return cand
    return ""


def _registry_map(platform: str, account_id: str) -> dict:
    """模板文件内容 md5 → 登记名。registry 里的 id 是随机 hex，不是 md5。"""
    import hashlib
    base = os.path.join(DATA, "platforms", platform, account_id, "emoji")
    _rp = os.path.join(base, "registry.json")
    if not os.path.isfile(_rp):
        # 2026-11-01：没登记过表情的账号本来就没有这个文件 —— 这不是异常。
        # 以前每次读都 warn，一小时 70+ 条，把体检的"静默告警"指标全占了。
        return {}
    try:
        with open(_rp, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        safelog.warn("sticker_registry_unreadable", base=base[-40:], err=repr(exc)[:120])
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, meta in data.items():
        if not isinstance(meta, dict):
            continue
        rel = str(meta.get("template") or "")
        if not rel:
            continue
        _rel = rel.replace("/", os.sep)
        fp = os.path.join(base, _rel)
        if not os.path.isfile(fp) and os.sep not in _rel:
            fp = os.path.join(base, "templates", _rel)   # 兼容只存文件名的旧条目
        if not os.path.isfile(fp):
            continue
        # 模板文件名本身就是那个表情的 md5（15b2bff339d7….png ↔ [表情包:15b2bff339d7…]）
        # registry 里有两层名字：key = 这张图画的是什么事，label = 情绪词。
        # 面板上显示 key（看图最认得出），把 label 一并带出去给 tooltip。
        stem = os.path.splitext(os.path.basename(fp))[0].lower()
        _pair = {"key": str(key), "label": str(meta.get("label") or "")}
        if re.fullmatch(r"[0-9a-f]{16,}", stem or ""):
            out[stem] = _pair
        # 兜底：内容 md5（万一哪天换了命名）
        try:
            with open(fp, "rb") as fh2:
                out.setdefault(hashlib.md5(fh2.read()).hexdigest(), _pair)
        except OSError as exc:
            safelog.warn("sticker_template_unreadable", file=fp[-48:], err=repr(exc)[:100])
            continue
    return out


#: 表情的"情绪"只认这 20 个词（受控词表）：自由填的话，她会用一个她自己也解释不清的词
FEEL_WORDS = ("招呼", "同意", "拒绝", "撒娇", "委屈", "心疼", "无语", "得意", "自嘲", "催促",
              "敷衍", "惊讶", "疑问", "道谢", "道歉", "生气", "打趣", "安慰", "发呆", "收尾")
#: 这张图能用在哪些场合
SCOPES = ("any", "owner", "group")


def _members_of(md5: str) -> list:
    """同一张图的全部本地文件（像素级同图，跨编码）。查不到就只返回它自己。"""
    try:
        from db import sticker_catalog as _sc
        ms = _sc.members_of(md5)
        return ms or [md5]
    except Exception:
        return [md5]


def sticker_register(md5: str, key: str, label: str = "", *, platform: str = "", account_id: str = "",
                     by: str = "panel", feel: str = "", intensity: int = 0,
                     scope: str = "any", taboo: bool = False) -> dict:
    """把本地缓存里的一张表情登记进这个账号的 registry（写 templates + registry.json）。

    口径：这是 E4（人工登记），不声称"她发过"。
    2026-11-01 加三轴：``feel``（20 个受控情绪词）、``intensity``（1–5 分量）、
    ``scope``（any / owner 只对主人 / group 只对群）与 ``taboo``（不适合严肃话题）。
    另外把"同图簇"的成员一起记进 ``members``：登记一张 = 登记整簇。
    """
    feel = (feel or "").strip()
    scope = (scope or "any").strip().lower()
    if feel and feel not in FEEL_WORDS:
        return {"ok": False, "error": "情绪只能是这 20 个词之一：%s" % "、".join(FEEL_WORDS)}
    if scope not in SCOPES:
        return {"ok": False, "error": "范围只能是 any / owner / group"}
    try:
        intensity = int(intensity or 0)
    except Exception:
        intensity = 0
    if intensity and not (1 <= intensity <= 5):
        return {"ok": False, "error": "强度只能 1–5（0 = 没标）"}
    import glob as _glob, shutil as _sh
    md5 = (md5 or "").strip().lower()
    key = (key or "").strip()
    if len(md5) < 16:
        return {"ok": False, "error": "md5 不合法"}
    if not key:
        return {"ok": False, "error": "图意（key）不能为空"}
    plat = (platform or "wechat").strip()
    acct = (account_id or "").strip()
    if not acct:
        cands = _glob.glob(os.path.join(DATA, "platforms", plat, "*", "emoji", "registry.json"))
        acct = os.path.basename(os.path.dirname(os.path.dirname(cands[0]))) if cands else ""
    base = os.path.join(DATA, "platforms", plat, acct, "emoji")
    reg_path = os.path.join(base, "registry.json")
    tpl_dir = os.path.join(base, "templates")
    src = ""
    for d in (os.path.join(DATA, "emoji_cache"), os.path.join(DATA, "image_cache")):
        for fp in _glob.glob(os.path.join(d, md5 + ".*")):
            src = fp
            break
        if src:
            break
    if not src:
        return {"ok": False, "error": "本地找不到这张图（md5 %s）" % md5[:12]}
    reg = {}
    if os.path.isfile(reg_path):
        try:
            with open(reg_path, encoding="utf-8") as fh:
                reg = json.load(fh) or {}
        except Exception as exc:
            return {"ok": False, "error": "registry 读不了：%r" % (exc,)}
    if key in reg:
        return {"ok": False, "error": "已经有一张叫「%s」了" % key}
    if any(isinstance(v, dict) and str(v.get("md5") or "").lower() == md5 for v in reg.values()):
        return {"ok": False, "error": "这张图已经登记过了"}
    os.makedirs(tpl_dir, exist_ok=True)
    # 2026-11-01：缓存文件是微信的 .dat 容器（内容其实是 png/gif），模板必须用真实图片扩展名，
    # 否则 _registry_map 按 png/jpg/gif/webp 扫模板目录时扫不到 → 登记了却回读成"未登记"。
    ext = os.path.splitext(src)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        try:
            from PIL import Image as _I
            _im = _I.open(src)
            _f = (_im.format or "").upper()
            ext = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp",
                   "BMP": ".png", "MPO": ".jpg"}.get(_f, ".png")
        except Exception:
            ext = ".png"
    dst = os.path.join(tpl_dir, md5 + ext)
    _sh.copy2(src, dst)
    reg[key] = {
        # 2026-11-01：模板路径必须带 templates/ 前缀——_registry_map 是 base + rel 拼的，
        # 只存文件名会拼成 base/xxx.png（不存在）→ 登记了却回读成"未登记"。
        "md5": md5, "label": (label or "").strip(),
        "template": "templates/" + os.path.basename(dst),
        "thumb": "templates/" + os.path.basename(dst), "aliases": [], "context": "",
        "scope": scope, "feel": feel, "intensity": intensity, "taboo": bool(taboo),
        "members": _members_of(md5),
        "added_by": by, "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    atomic_write_json(reg_path, reg, indent=2)
    return {"ok": True, "key": key, "md5": md5, "file": dst, "total": len(reg)}


def sticker_forget(md5: str, *, platform: str = "", account_id: str = "", by: str = "panel") -> dict:
    """从 registry 里撤掉一张（不动本地缓存文件）。"""
    import glob as _glob
    md5 = (md5 or "").strip().lower()
    plat = (platform or "wechat").strip(); acct = (account_id or "").strip()
    if not acct:
        cands = _glob.glob(os.path.join(DATA, "platforms", plat, "*", "emoji", "registry.json"))
        acct = os.path.basename(os.path.dirname(os.path.dirname(cands[0]))) if cands else ""
    reg_path = os.path.join(DATA, "platforms", plat, acct, "emoji", "registry.json")
    if not os.path.isfile(reg_path):
        return {"ok": False, "error": "没有 registry"}
    with open(reg_path, encoding="utf-8") as fh:
        reg = json.load(fh) or {}
    gone = [k for k, v in reg.items() if isinstance(v, dict) and str(v.get("md5") or "").lower() == md5]
    for k in gone:
        reg.pop(k, None)
    if not gone:
        return {"ok": False, "error": "这张没登记过"}
    atomic_write_json(reg_path, reg, indent=2)
    return {"ok": True, "removed": gone, "total": len(reg)}

def sticker_registered(platform: str = "", account_id: str = "") -> dict:
    """池子现状：她能真正发出去的那批（registry 里的），带缩略图与证据等级。
    E3 = 我们自己的平台回执能对上（查 yinyue_meta.send_log，表不在就降级为 E4）。
    E4 = 人工登记（registry 里有）。"""
    import glob as _glob, sqlite3 as _sq
    plat = (platform or "wechat").strip()
    acct = (account_id or "").strip()
    if not acct:
        cands = _glob.glob(os.path.join(DATA, "platforms", plat, "*", "emoji", "registry.json"))
        acct = os.path.basename(os.path.dirname(os.path.dirname(cands[0]))) if cands else ""
    base = os.path.join(DATA, "platforms", plat, acct, "emoji")
    reg_path = os.path.join(base, "registry.json")
    tpl_dir = os.path.join(base, "templates")
    if not os.path.isfile(reg_path):
        return {"ok": True, "platform": plat, "account_id": acct, "total": 0, "rows": [],
                "note": "这个账号还没有 registry"}
    with open(reg_path, encoding="utf-8") as fh:
        reg = json.load(fh)
    proven = set()
    try:
        con = _sq.connect(os.path.join(DATA, "yinyue_meta.db"))
        for (mid,) in con.execute("select confirmed_msg_id from send_log where confirmed_msg_id is not null"):
            if mid:
                proven.add(str(mid))
        con.close()
    except Exception as exc:
        log.warning("sticker_proven_lookup_failed", err=repr(exc))
    files = {}
    if os.path.isdir(tpl_dir):
        for fp in _glob.glob(os.path.join(tpl_dir, "*")):
            files[os.path.splitext(os.path.basename(fp))[0].lower()] = fp
    rows = []
    for key, ent in (reg or {}).items():
        if not isinstance(ent, dict):
            continue
        md5 = str(ent.get("template") or ent.get("thumb") or "").split("/")[-1].split(".")[0].lower()
        fp = files.get(md5) or ""
        rows.append({
            "key": key,
            "label": ent.get("label") or "",
            "md5": md5,
            "thumb": _sticker_thumb(fp) if fp else "",
            "file": fp,
            "evidence": "E4",
            "taboo": ent.get("context") or "",
            "aliases": ent.get("aliases") or [],
            "note": "人工登记" + ("" if proven else "（暂无发送回执可对账）"),
        })
    return {"ok": True, "platform": plat, "account_id": acct,
            "total": len(rows), "proven": len(proven), "rows": rows}


_STICKER_PX = {}          # md5 -> 32x32 归一化像素哈希（进程内缓存，避免每次重算）
_STICKER_DH = {}          # md5 -> dhash 64bit


def _registry_map_any() -> dict:
    """没指定平台/账号时，把所有账号的 registry 并起来（面板就是"这个人的机子上有什么"）。"""
    out = {}
    base = os.path.join(DATA, "platforms")
    try:
        for plat in sorted(os.listdir(base)):
            pdir = os.path.join(base, plat)
            if not os.path.isdir(pdir):
                continue
            for acct in sorted(os.listdir(pdir)):
                if os.path.isdir(os.path.join(pdir, acct, "emoji")):
                    out.update(_registry_map(plat, acct))
    except Exception as exc:
        safelog.warn("registry_map_any_failed", err=repr(exc))
    return out

def _sticker_sig(path: str) -> tuple:
    """返回 (像素哈希, dhash)。像素哈希 = 转 RGB 缩到 32x32 后的 md5——纯字节无关，
    同一张图换个编码/压缩也会相同；dhash 只用于"建议同组"，不自动合并。"""
    try:
        from PIL import Image
        im = Image.open(path)
        if getattr(im, "is_animated", False):
            im.seek(0)
        rgb = im.convert("RGB").resize((32, 32), Image.BILINEAR)
        px = hashlib.md5(rgb.tobytes()).hexdigest()[:16]
        grey = im.convert("L").resize((9, 8), Image.BILINEAR)
        data = list(grey.getdata())
        h = 0
        for r in range(8):
            for c in range(8):
                h = (h << 1) | (1 if data[r * 9 + c] > data[r * 9 + c + 1] else 0)
        return px, h
    except Exception as exc:
        safelog.warn("sticker_sig_failed", path=path, err=repr(exc))
        return "", 0


def _sticker_groups(rows: list) -> list:
    """把 rows 按"像素级同图"分组，返回 [[row, ...], ...]；每组按 registered/大小取代表。"""
    buckets = {}
    for r in rows:
        px, dh = _sticker_sig(r["path"])
        r["sig"] = px
        r["dhash"] = dh
        key = px or ("solo:" + r["md5"])
        buckets.setdefault(key, []).append(r)
    return list(buckets.values())

def sticker_pool(platform: str = "", account_id: str = "", limit: int = 5000) -> dict:
    """本地表情库（2026-11-01 B 案）：不再猜"她发过哪些"，直接列本地缓存里的全部表情，
    已登记的打标。来源分不清就是分不清，与其标一个错的"实证"，不如给一个能用的图库。"""
    import glob as _glob
    cache_dir = os.path.join(DATA, "emoji_cache")
    img_dir = os.path.join(DATA, "image_cache")
    reg = _registry_map(platform, account_id) if (platform or account_id) else _registry_map_any()
    rows = []
    seen = set()
    for d in (cache_dir, img_dir):
        if not os.path.isdir(d):
            continue
        for fp in _glob.glob(os.path.join(d, "*")):
            # 分类闸（2026-11-01）：emoji_cache 是微信表情容器（.dat），全收；
            # image_cache 混着聊天照片/截图，只收动图（gif/webp），静态图一律不进表情库。
            if d == img_dir and os.path.splitext(fp)[1].lower() not in (".gif", ".webp"):
                continue
            md5 = os.path.splitext(os.path.basename(fp))[0].lower()
            if md5 in seen or len(md5) < 16:
                continue
            seen.add(md5)
            hit = reg.get(md5) or {}
            rows.append({
                "md5": md5,
                "registered": bool(hit),
                "registered_key": hit.get("key") or "",
                "registered_label": hit.get("label") or "",
                "registered_feel": hit.get("feel") or "",
                "registered_intensity": int(hit.get("intensity") or 0),
                "registered_scope": hit.get("scope") or "any",
                "registered_taboo": bool(hit.get("taboo")),
                "members": len(hit.get("members") or []) or 1,
                "thumb": "/thumb/" + md5,
                "has_thumb": bool(fp and os.path.getsize(fp) > 0),
                "path": fp,
                "size": os.path.getsize(fp) if os.path.exists(fp) else 0,
            })
            if len(rows) >= max(1, int(limit)):
                break
        if len(rows) >= max(1, int(limit)):
            break
    rows.sort(key=lambda r: (0 if r["registered"] else 1, r["md5"]))
    groups = _sticker_groups(rows)
    reps = []
    for g in groups:
        # 代表优先取"已登记的那个成员"——否则登记的 md5 与展示的 md5 不同，回读会显示未登记
        g.sort(key=lambda r: (0 if r["registered"] else 1, -int(r.get("size") or 0), r["md5"]))
        rep = g[0]
        rep["dup_count"] = len(g)
        rep["dup_others"] = [x["md5"] for x in g[1:]]
        rep["dup_registered"] = [x["registered_key"] for x in g[1:] if x["registered"]]
        reps.append(rep)
    reps.sort(key=lambda r: (0 if r["registered"] else 1, r["md5"]))
    files_total = len(rows)
    return {"ok": True, "platform": platform, "account_id": account_id,
            "feels": list(FEEL_WORDS), "scopes": list(SCOPES),
            "total": len(reps), "files": files_total,
            "dups": files_total - len(reps),
            "registered": sum(1 for r in reps if r["registered"]),
            "rows": reps}

def diagnose_dump(*, note: str = "", by: str = "panel", tail_lines: int = 200) -> dict:
    # 2026-11-01：把现场打包成一个文件，比截图强十倍。
    # 主人说"又不行了"的时候我每次都要问一堆问题（哪一页、几点、什么报错）。
    # 这个命令把心跳、日志尾巴、体检结果、队列尾部、进程表一次性落盘。
    import json as _json
    import sqlite3 as _sq
    import time as _t
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "data", "console")
    os.makedirs(out_dir, exist_ok=True)
    stamp = _t.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(out_dir, "diagnose-%s.json" % stamp)
    pack = {"at": _t.strftime("%Y-%m-%d %H:%M:%S"), "by": by, "note": note, "sections": {}}

    try:
        import psutil as _ps
        procs = []
        for p in _ps.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                cl = " ".join(p.info.get("cmdline") or [])
            except Exception as exc:
                # 有些进程（权限不够/刚好退出）读不到 cmdline，记下来别静默丢
                procs.append({"pid": p.info.get("pid"), "error": repr(exc)[:80]})
                continue
            if any(k in cl for k in ("loop.run", "loop.desk", "desk_supervisor", "scheduler/main.py",
                                     "console/collector", "supervisor.py")):
                procs.append({"pid": p.info["pid"],
                              "started": _t.strftime("%H:%M:%S", _t.localtime(p.info.get("create_time") or 0)),
                              "cmd": cl[:160]})
        pack["sections"]["processes"] = procs
    except Exception as exc:
        pack["sections"]["processes"] = {"error": repr(exc)[:200]}

    try:
        from console import checkup as _ck
        _r = _ck.run()
        pack["sections"]["checkup"] = {
            "level": _r.get("level"), "headline": _r.get("headline"), "counts": _r.get("counts"),
            "checks": [{"id": c.get("id"), "level": c.get("level"), "line": c.get("line")}
                       for c in (_r.get("checks") or [])]}
    except Exception as exc:
        pack["sections"]["checkup"] = {"error": repr(exc)[:200]}

    try:
        con = _sq.connect("file:%s?mode=ro" % os.path.join(root, "data", "world.db"), uri=True)
        con.row_factory = _sq.Row
        rows = [dict(r) for r in con.execute(
            "select id,platform,chat_id,kind,state,attempts,ack,why,created_ts from outbound_queue "
            "order by id desc limit 15")]
        counts = {r["state"]: r["n"] for r in con.execute(
            "select state, count(*) n from outbound_queue group by state")}
        con.close()
        pack["sections"]["queue"] = {"counts": counts, "tail": rows}
    except Exception as exc:
        pack["sections"]["queue"] = {"error": repr(exc)[:200]}

    try:
        lg = os.path.join(root, "logs", "yinyue.log")
        lines = []
        if os.path.exists(lg):
            # 2026-11-01：原来整读（yinyue.log 已 20MB）只为留最后 200 行 ——
            # 改成只读尾部 512KB（200 行用不了这么多），语义不变。
            _size = os.path.getsize(lg)
            with open(lg, "rb") as fh:
                if _size > 512 * 1024:
                    fh.seek(_size - 512 * 1024)
                    fh.readline()          # 丢掉被截断的半行
                _raw = fh.read()
            lines = _raw.decode("utf-8", "replace").splitlines()[-int(tail_lines):]
        pack["sections"]["log_tail"] = {"file": "logs/yinyue.log", "lines": lines}
    except Exception as exc:
        pack["sections"]["log_tail"] = {"error": repr(exc)[:200]}

    try:
        import json as _j
        hb = {}
        for nm in sorted(liveness.heartbeat_names().values()):  # 2026-11-02：名字只许有一处出处
            fp = os.path.join(root, "logs", nm)
            if os.path.exists(fp):
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    hb[nm] = _j.load(fh)
        pack["sections"]["heartbeat"] = hb
    except Exception as exc:
        pack["sections"]["heartbeat"] = {"error": repr(exc)[:200]}

    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        _json.dump(pack, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    return {"ok": True, "path": os.path.relpath(out, root).replace(os.sep, "/"),
            "size": os.path.getsize(out), "at": pack["at"]}


def _stop_files() -> tuple:
    """四个 stop 文件。做成函数而非常量：测试要改路径，常量会在 import 时被固化。"""
    return _all_stops()


def _stop_snapshot() -> list:
    """动手前就已经存在的 stop 文件（绝对路径），只有回滚时才有用。"""
    return [p for p in _stop_files() if os.path.exists(p)]


def _stop_rollback(existed: list) -> list:
    """把这次新写、且不在 existed 里的 stop 文件删掉；返回动了哪些（相对路径）。"""
    undone = []
    for path in _stop_files():
        if path in existed or not os.path.exists(path):
            continue
        try:
            os.remove(path)
            undone.append(os.path.relpath(path, ROOT))
        except OSError as exc:
            undone.append("%s（没删掉：%s）" % (os.path.relpath(path, ROOT), exc))
    return undone


def _still_running() -> list:
    """还没退的都有谁（给人看的中文名）。"""
    bad = []
    if _port_open(ENGINE_PORT):
        bad.append("引擎端口 %d" % ENGINE_PORT)
    if liveness.pid_alive(liveness.read("scheduler").get("pid")):
        bad.append("主程序")
    if _engine_pid():
        bad.append("AI 引擎")
    if _desk_pid():
        bad.append("发送工 desk")
    return bad


def _wait_gone(wait_s: float) -> tuple:
    """等全套退干净，返回 (是否退干净, 等了多久, 还活着的)。"""
    t0 = time.time()
    end = t0 + max(0.0, float(wait_s))
    while True:
        bad = _still_running()
        if not bad:
            return True, round(time.time() - t0, 1), []
        if time.time() >= end:
            return False, round(time.time() - t0, 1), bad
        time.sleep(0.5)


def svc_stop(*, by: str = "panel", wait_s: float = 45.0) -> dict:
    """按下全关（四个哨兵），后台核对有没有真退干净。"""
    import threading

    text = "console %s by %s" % (_now(), by)
    before = _stop_snapshot()
    wrote = []
    for path in _all_stops():
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            wrote.append(os.path.relpath(path, ROOT))
        except OSError as exc:
            undone = _stop_rollback(before)
            note = "写不了 %s（%s）" % (os.path.relpath(path, ROOT), exc)
            _svc_note("svc_stop", False, note)
            return {"ok": False, "rolled_back": True, "undone": undone, "error": note}
    row = {"id": _id(), "at": _now(), "by": by, "cmd": "svc_stop", "target": "",
           "path": "logs/all.stop", "summary": "停止全部", "backup": "", "diff": "",
           "note": "写下 " + "、".join(wrote)}
    _audit_row(row)

    # 2026-11-03：把它要用的函数在**按下按钮那一刻**绑住。这个线程是 daemon，
    # 可能在测试已经撤销 monkeypatch 之后才拿到 GIL —— 那时全局名指回真函数，
    # 它会真的等 45 秒、甚至把结果写进别人的账本（test_svc_buttons 因此偶发红）。
    wait_gone = _wait_gone

    def _verify() -> None:
        clean, waited, bad = wait_gone(wait_s)
        if clean:
            _svc_note("svc_stop", True, "已全关：等了 %.1f 秒，端口释放、主程序 / 引擎 / 发送工都退了" % waited)
        else:
            _svc_note("svc_stop", False, "按下全关 %.0f 秒后还活着：%s" % (waited, "、".join(bad)))

    threading.Thread(target=_verify, name="svc-stop-verify", daemon=True).start()
    note = ("已按下全关（%s）。\n范围＝监管器 + 主程序 + AI 引擎 + 发送工 desk + 外部看门狗（watchdog.stop）；"
            "面板会留着。约 10 秒后这行会变成「已全关」或「还活着：…」。" % "、".join(wrote))
    return {"ok": True, "audit_id": row["id"], "existed": before, "note": note}


def _wait_released(wait_s: float) -> tuple:
    """等「引擎端口 39871 关掉 + 调度器进程退掉」，返回 (是否等到, 等了多久秒)。

    超时也照常返回：超时算不算失败由调用方决定，不许在这里悄悄变成「继续往下走」。"""
    t0 = time.time()
    end = t0 + max(0.0, float(wait_s))
    while time.time() < end:
        if not _port_open(ENGINE_PORT) and not liveness.pid_alive(liveness.read("scheduler").get("pid")):
            return True, round(time.time() - t0, 2)
        time.sleep(0.5)
    return False, round(time.time() - t0, 2)


def _wait_engine_up(deadline: float) -> tuple:
    """等「引擎端口通了 + 已经跑过至少一拍」，返回 (是否等到, 等了多久秒)。

    2026-10-31：这段原来写在 svc_restart 里的 while 循环中，但 `test_restart_rollback.py`
    钉死「svc_restart 里不许有 while，等释放只许走 _wait_released」——为了既守住结构锁
    又保留"等真的起来了才报成功"，把等待挪到这里。超时与否由调用方决定怎么报。"""
    t0 = time.time()
    while time.time() < deadline:
        if _port_open(ENGINE_PORT) and (liveness.read("engine").get("ticks") or 0) >= 1:
            return True, round(time.time() - t0, 1)
        time.sleep(1.0)
    return False, round(time.time() - t0, 1)


def svc_restart(*, by: str = "panel", wait_s: float = 90.0) -> dict:
    """停 → 等 39871 释放 → 起。全程后台做，面板立刻拿到回执。"""
    import subprocess
    import threading

    row = {"id": _id(), "at": _now(), "by": by, "cmd": "svc_restart", "target": "",
           "path": "logs/all.stop", "summary": "重启全部（停→等→起）", "backup": "",
           "diff": "", "note": ""}
    _audit_row(row)
    started = svc_stop(by=by)
    if not started.get("ok"):
        _svc_note("svc_restart", False, "没能按下停止：%s" % (started.get("error") or ""))
        return started
    before = list(started.get("existed") or [])

    def _runner() -> None:
        released, waited = _wait_released(wait_s)
        if not released:
            undone = _stop_rollback(before)
            note = ("重启失败：等了 %.0f 秒端口/主程序还没放掉，已把这次写的哨兵撤回（%s）。"
                    "现在既没停稳也没起成，点「停止全部」再「启动全部」最稳。"
                    % (waited, "、".join(undone) or "无"))
            _svc_note("svc_restart", False, note)
            from safelog import warn
            warn("svc_restart_wait_timeout", waited_s=waited, undone=undone)
            return
        time.sleep(0.3)
        removed, errors = _clear_stops()
        if errors:
            _svc_note("svc_restart", False, "重启失败：哨兵删不掉（%s）" % "、".join(errors))
            return
        try:
            proc = subprocess.Popen([PYW, SUPERVISOR], cwd=ROOT, creationflags=_DETACHED,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True)
        except Exception as exc:  # noqa: BLE001
            _svc_note("svc_restart", False, "重启失败：拉不起监管器（%r）" % (exc,))
            return
        # 2026-10-31：不再「拉起来就报成功」——等端口真的 LISTENING + 有新的一次 tick。
        # 等待本身放在 _wait_engine_up 里（svc_restart 里不许有 while，见 test_restart_rollback）。
        _pid = int(getattr(proc, "pid", 0) or 0)
        up, waited = _wait_engine_up(time.time() + 120.0)
        if up:
            _col = _ensure_collector()
            _extra = ""
            if _col.get("started"):
                _extra = "；面板后端也拉起来了（pid %s）" % _col.get("pid")
            elif not _col.get("ok"):
                _extra = "；但面板后端没起来（%s）" % (_col.get("error") or "未知")
            _svc_note("svc_restart", True, "重启完成：监管器 pid %d，引擎端口 %d 已通，用了 %.0f 秒%s"
                      % (_pid, ENGINE_PORT, waited, _extra))
            return
        _svc_note("svc_restart", False, "监管器 pid %d 拉起来了，但 120 秒内端口 %d 没通 —— 看 logs/supervisor.out"
                  % (_pid, ENGINE_PORT))

    threading.Thread(target=_runner, name="svc-restart", daemon=True).start()
    note = "已开始重启：先停（等端口释放，最多 %d 秒）再拉监管器；结果会出现在「上次操作」那盏灯上" % int(wait_s)
    return {"ok": True, "audit_id": row["id"], "note": note}


# ---- 角色卡：切卡 / 新建卡 / 声音与外观（2026-10-27） ------------------------
# 事实源就是文件：data/persona/cards/<名字>.json + data/persona/active.json。
# 这里只做「选哪张、抄一张新的、看音色包在不在」，人设怎么编译归 loop/cards.py。

# ---- 2026-10-27：卡改成「一整块自由文本」（<名字>.md）----
# 用户要求：整个卡片就是一块提示词，不拆类别、不做归类，结构完全由写卡人排版决定。
# 程序只认头部三行（name/voice/looks），正文一个字不改地整块注入给模型。
_MD_CAP = 20000          # 与 loop/selfcard.py 的 _CARD_MD_CHARS 保持一致
_MD_HEADER = ("name", "voice", "looks")


def _md_path(nm: str) -> str:
    return os.path.join(PERSONA_DIR, nm + ".md")


def _md_split(text: str):
    """切成 (头部键值, 正文)。头部 = 第一段 `---` 之前，一行一个 `键: 值`。"""
    if "\n---" not in "\n" + str(text or ""):
        return {}, str(text or "").strip()
    head, body = ("\n" + str(text or "")).split("\n---", 1)
    meta = {}
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip().lower()] = v.strip()
    return meta, body.strip()


def _md_join(meta: dict, body: str) -> str:
    head = ["%s: %s" % (k, str((meta or {}).get(k) or "").strip()) for k in _MD_HEADER]
    return "\n".join(head + ["---"]) + "\n" + str(body or "").strip() + "\n"


def _md_cards() -> set:
    """磁盘上有 .md 的卡名（同名 .json 会被它顶掉，不再重复列出）。"""
    d = PERSONA_DIR
    if not os.path.isdir(d):
        return set()
    return {f[: -len(".md")] for f in os.listdir(d)
            if f.lower().endswith(".md") and ".bak" not in f.lower()}


def card_list() -> dict:
    out = []
    d = PERSONA_DIR
    if os.path.isdir(d):
        _mds = _md_cards()
        for f in sorted(os.listdir(d)):
            _low = f.lower()
            if ".bak" in _low:
                continue
            if _low.endswith(".md"):
                path = os.path.join(d, f)
                name = f[: -len(".md")]
                try:
                    meta, _body = _md_split(open(path, encoding="utf-8").read())
                except Exception:  # noqa: BLE001 - 坏卡也要能看见
                    meta, _body = {}, ""
                out.append({"name": name,
                            "card_name": str(meta.get("name") or name),
                            "identity": _body[:70].replace("\n", " "),
                            "looks": str(meta.get("looks") or ""),
                            "voice": str(meta.get("voice") or ""),
                            "bytes": os.path.getsize(path),
                            "chars": len(_body),
                            "kind": "md",
                            "active": name == active_card_name()})
                continue
            if not _low.endswith(".json") or f[: -len(".json")] in _mds:
                continue
            path = os.path.join(d, f)
            try:
                c = _load(path)
            except Exception:  # noqa: BLE001 - 坏卡也要能看见，方便修
                c = {}
            name = f[: -len(".json")]
            out.append({"name": name,
                        "card_name": str(c.get("name") or name),
                        "identity": str(c.get("identity") or "")[:70],
                        "looks": str(c.get("looks") or ""),
                        "voice": str(c.get("voice") or ""),
                        "bytes": os.path.getsize(path),
                        "kind": "json",
                        "active": name == active_card_name()})
    return {"ok": True, "active": active_card_name(), "cards": out,
            "dir": os.path.relpath(d, ROOT)}


def _card_ok(name: str) -> tuple:
    nm = str(name or "").strip()
    if nm.endswith(".json"):
        nm = nm[: -len(".json")]
    if not nm:
        return False, "卡名不能为空", ""
    if len(nm) > 20:
        return False, "卡名最多 20 字", ""
    if os.sep in nm or "/" in nm or ":" in nm:
        return False, "卡名不能带路径分隔符", ""
    return True, "", nm


def card_switch(name: str, *, dry_run: bool = True, by: str = "panel") -> dict:
    ok, err, nm = _card_ok(name)
    if not ok:
        return {"ok": False, "error": err}
    path = os.path.join(PERSONA_DIR, nm + ".json")
    if not os.path.exists(path):
        return {"ok": False, "error": "没有这张卡：%s（%s）" % (nm, os.path.relpath(PERSONA_DIR, ROOT))}
    before = _load(ACTIVE) if os.path.exists(ACTIVE) else {}
    after = {"card": nm}
    before_text, after_text = _dump(before), _dump(after)
    if str(before.get("card") or "") == nm:
        return {"ok": True, "dry_run": dry_run, "noop": True, "summary": "已经是这张卡",
                "active": nm, "diff": ""}
    diff = _diff(before_text, after_text, "active.json")
    if dry_run:
        return {"ok": True, "dry_run": True, "diff": diff, "active": nm,
                "summary": "把生效卡换成 %s（还不写盘）" % nm}
    backup = _backup(ACTIVE, "active")
    _atomic_write(ACTIVE, after_text)
    row = {"id": _id(), "at": _now(), "by": by, "cmd": "card_switch", "target": "active",
           "path": os.path.relpath(ACTIVE, ROOT), "summary": "生效卡 → %s" % nm,
           "changed": ["card"], "before_sha": _sha(before_text), "after_sha": _sha(after_text),
           "backup": os.path.relpath(backup, ROOT), "diff": diff[:MAX_AUDIT_DIFF]}
    _audit_row(row)
    return {"ok": True, "dry_run": False, "diff": diff, "active": nm, "audit_id": row["id"],
            "summary": "生效卡 → %s" % nm,
            "note": "已写入。她下一拍就按这张卡说话（不用重启）。"}


def card_new(name: str, *, source: str = "", overwrite: bool = False, by: str = "panel") -> dict:
    """抄一张卡。2026-10-27：卡只有一种格式 —— 一整块散文（<名字>.md + 头部三行）。"""
    from core.identity import cardfile as _cardfile
    ok, err, nm = _card_ok(name)
    if not ok:
        return {"ok": False, "error": err}
    src = str(source or active_card_name() or "").strip()
    for suf in (".md", ".json"):
        if src.endswith(suf):
            src = src[: -len(suf)]
    src_path = _md_path(src) if src else ""
    if not src_path or not os.path.exists(src_path):
        return {"ok": False, "error": "底稿不存在：%s（卡都是 .md）" % (src or "(没有生效卡)")}
    dst = _md_path(nm)
    if os.path.exists(dst) and not overwrite:
        return {"ok": False, "error": "已经有这张卡了：%s（换个名字，或确认覆盖）" % nm}
    try:
        meta, body = _md_split(open(src_path, encoding="utf-8").read())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "底稿读不出来：%s: %s" % (type(exc).__name__, exc)}
    meta = dict(meta or {})
    meta["name"] = nm
    body = ("（从「%s」抄来的一张新卡。下面随便改：排版、顺序、加减小节都行，"
            "程序一个字不改地整块给她看。）\n\n" % src) + body
    text = _cardfile.join(meta, body)
    backup = ""
    if os.path.exists(dst):
        backup = _backup(dst, "persona")
    os.makedirs(PERSONA_DIR, exist_ok=True)
    _atomic_write(dst, text)
    row = {"id": _id(), "at": _now(), "by": by, "cmd": "card_new", "target": "persona",
           "path": os.path.relpath(dst, ROOT), "summary": "新建卡 %s（底稿 %s）" % (nm, src),
           "changed": ["new"], "before_sha": "", "after_sha": _sha(text),
           "backup": os.path.relpath(backup, ROOT) if backup else "", "diff": ""}
    _audit_row(row)
    return {"ok": True, "path": os.path.relpath(dst, ROOT), "bytes": len(text),
            "chars": len(body), "audit_id": row["id"],
            "summary": "新建卡 %s（底稿 %s）" % (nm, src),
            "note": "卡已建好，但还没生效；要她现在就用这张，点「切到这张卡」。"}

_CARD_SCHEMA_CACHE: dict = {}


def _card_schema() -> dict:
    """人设卡能改的字段：**以 loop/cards.py 的 SCHEMA 为准**（那里才是卡的真理），
    取不到就退回本模块 SCHEMA 里 persona.* 那部分。"""
    if _CARD_SCHEMA_CACHE:
        return _CARD_SCHEMA_CACHE
    out: dict = {}
    try:
        from loop.cards import LIST_FIELDS, SCHEMA as _CS

        lines = tuple(LIST_FIELDS)
        for key, rule in _CS.items():
            out[key] = {"cap": int(rule.get("cap") or 120),
                        "max_items": int(rule.get("max_items") or 8),
                        "lines": key in lines,
                        "use": str(rule.get("use") or "")}
    except Exception:  # noqa: BLE001 - 退回本模块白名单
        for full, rule in SCHEMA.items():
            if not full.startswith("persona."):
                continue
            key = full.split(".", 1)[1]
            kind = rule.get("kind") or "text"
            out[key] = {"cap": int(rule.get("max") or rule.get("max_len") or 120),
                        "max_items": int(rule.get("max_items") or 8),
                        "lines": kind == "lines",
                        "use": str(rule.get("label") or "")}
    for key, rule in out.items():
        full = "persona.%s" % key
        if full in SCHEMA and SCHEMA[full].get("label"):
            rule["label"] = SCHEMA[full]["label"]
        else:
            rule.setdefault("label", key)
    _CARD_SCHEMA_CACHE.update(out)
    return out


def _card_check(key: str, value):
    """按卡片 schema 校验/规范化一个字段。返回 (ok, 值 or 错误说明)。"""
    rule = _card_schema().get(key)
    if rule is None:
        return False, "这张卡里没有这个字段：%s" % key
    cap = int(rule.get("cap") or 120)
    if isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
        items = [x for x in items if x]
        if len(items) > int(rule.get("max_items") or 8):
            return False, "最多 %d 条" % int(rule.get("max_items") or 8)
        for x in items:
            if len(x) > cap:
                return False, "每条最多 %d 字（现在 %d）" % (cap, len(x))
        return True, items
    text = str(value if value is not None else "").strip()
    if len(text) > cap:
        return False, "最多 %d 字（现在 %d）" % (cap, len(text))
    if rule.get("lines") and "\n" in text:
        return True, [x.strip() for x in text.splitlines() if x.strip()]
    return True, text


def card_show(name: str = "") -> dict:
    """打开一张卡看详情（只读）：字段原值 + 文件名/大小/是否生效。"""
    ok, err, nm = _card_ok(name or active_card_name())
    if not ok:
        return {"ok": False, "error": err}
    path = os.path.join(PERSONA_DIR, nm + ".json")
    if not os.path.exists(path):
        return {"ok": False, "error": "没有这张卡：%s" % nm}
    try:
        data = _load(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "这张卡读不出来：%s: %s" % (type(exc).__name__, exc)}
    rule = _card_schema()
    fields, labels, kinds, caps, uses = {}, {}, {}, {}, {}
    for key in rule:
        val = data.get(key)
        fields[key] = val
        labels[key] = rule[key].get("label") or key
        uses[key] = rule[key].get("use") or ""
        caps[key] = int(rule[key].get("cap") or 0)
        # 类型按"盘上到底是什么"来给面板：list → 多行，其它 → 单行；rate 是表，不给编辑
        if key == "rate" and isinstance(val, list):
            kinds[key] = "rate"
        elif isinstance(val, list):
            kinds[key] = "lines"
        else:
            kinds[key] = "text"
    return {"ok": True, "name": nm, "card_name": str(data.get("name") or nm),
            "active": nm == active_card_name(),
            "bytes": os.path.getsize(path), "path": os.path.relpath(path, ROOT),
            "fields": fields, "labels": labels, "kinds": kinds, "caps": caps, "uses": uses}


def card_save(name: str, patch: dict, *, dry_run: bool = True, by: str = "panel") -> dict:
    """改一张卡里的字段（不一定是生效卡）。校验规则与"改生效卡"完全同一套 `_check`。"""
    ok, err, nm = _card_ok(name or active_card_name())
    if not ok:
        return {"ok": False, "error": err}
    path = os.path.join(PERSONA_DIR, nm + ".json")
    if not os.path.exists(path):
        return {"ok": False, "error": "没有这张卡：%s" % nm}
    try:
        before = _load(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "这张卡读不出来：%s: %s" % (type(exc).__name__, exc)}
    after = dict(before)
    for key, value in (patch or {}).items():
        good, res = _card_check(key, value)
        if not good:
            label = (_card_schema().get(key) or {}).get("label") or key
            return {"ok": False, "error": "%s：%s" % (label, res)}
        after[key] = res
    before_text, after_text = _dump(before), _dump(after)
    fname = os.path.basename(path)
    if before_text == after_text:
        return {"ok": True, "dry_run": dry_run, "noop": True, "summary": "没有改动", "name": nm}
    diff = _diff(before_text, after_text, fname)
    summary = "改卡 %s（%s）" % (nm, "、".join(sorted((patch or {}).keys()))[:80])
    if dry_run:
        return {"ok": True, "dry_run": True, "diff": diff, "name": nm, "summary": summary}
    backup = _backup(path, "card")
    _atomic_write(path, after_text)
    active = nm == active_card_name()
    row = {"id": _id(), "at": _now(), "by": by, "cmd": "card_save", "target": "persona",
           "path": os.path.relpath(path, ROOT), "summary": summary,
           "changed": sorted((patch or {}).keys()), "before_sha": _sha(before_text),
           "after_sha": _sha(after_text), "backup": os.path.relpath(backup, ROOT),
           "diff": diff[:MAX_AUDIT_DIFF]}
    _audit_row(row)
    return {"ok": True, "dry_run": False, "diff": diff, "name": nm, "audit_id": row["id"],
            "summary": summary, "active": active,
            "note": ("她这张就是生效卡，下一拍就按新写法说话。" if active else
                     "这不是生效卡，改完不影响她说话；要她用这张，点「切到这张卡」。")}


def _asset_dir(slug: str, kind: str) -> str:
    try:
        from core.assets import paths as _ap
        return str(_ap.kind_path(slug, kind))
    except Exception:  # noqa: BLE001
        return os.path.join(DATA, "persona", "assets", "private", slug, kind)


def voice_info(slug: str = "") -> dict:
    name = str(slug or "").strip()
    card = {}
    try:
        card = _load(persona_path())
    except Exception:  # noqa: BLE001
        card = {}
    vslug = name or str(card.get("voice") or "")
    lslug = str(card.get("looks") or "")
    info = {"ok": True, "card": active_card_name(), "voice_slug": vslug, "looks_slug": lslug,
            "voice_pack": False, "looks_pack": False, "canon_chars": 0, "engine": "",
            "voice": "", "rate": "", "pitch": "", "style": "", "engines": {}, "faces": 0}
    try:
        from core.speech import tts_engine, voice_pack
        info["engines"] = {e: bool(tts_engine.available(e)) for e in tts_engine.ENGINES}
        if vslug:
            vdir = _asset_dir(vslug, "voice")
            info["voice_pack"] = os.path.isfile(os.path.join(vdir, "pack.json"))
            pack = voice_pack.load_pack(vslug) or {}
            for k in ("engine", "voice", "rate", "pitch", "style"):
                info[k] = str(pack.get(k) or "")
            canon = os.path.join(vdir, "canon.txt")
            if os.path.isfile(canon):
                try:
                    info["canon_chars"] = len(io.open(canon, encoding="utf-8").read().strip())
                except OSError:
                    info["canon_chars"] = 0
        if lslug:
            fdir = _asset_dir(lslug, "faces")
            info["looks_pack"] = os.path.isfile(os.path.join(fdir, "pack.json"))
            idx = os.path.join(fdir, "index.json")
            if os.path.isfile(idx):
                try:
                    info["faces"] = int((_load(idx) or {}).get("count") or 0)
                except Exception:  # noqa: BLE001
                    info["faces"] = 0
    except Exception as exc:  # noqa: BLE001
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
    return info


def voice_preview(slug: str = "", text: str = "") -> dict:
    """合成一段音色样音（**只合成，不发送**）。返回文件路径供面板 <audio> 播放。"""
    s = str(slug or "").strip()
    try:
        from core.speech import tts_engine, voice_pack
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "语音模块不可用：%s: %s" % (type(exc).__name__, exc)}
    pack = {}
    try:
        pack = voice_pack.load_pack(s) or {}
    except Exception:  # noqa: BLE001
        pack = {}
    say = str(text or "").strip()
    if not say:
        try:
            say = str(voice_pack.canon_text(s) or "").strip()
        except Exception:  # noqa: BLE001
            say = ""
    if not say:
        say = "我是%s，试一下声音。" % (active_card_name() or "她")
    say = say[:120]
    out_dir = os.path.join(DATA, "console", "voice_preview")
    os.makedirs(out_dir, exist_ok=True)
    key = hashlib.md5(("%s|%s|%s" % (s, say, pack)).encode("utf-8")).hexdigest()[:10]
    out_path = os.path.join(out_dir, "%s-%s.wav" % (s or "default", key))
    cached = os.path.exists(out_path) and os.path.getsize(out_path) > 100
    if not cached:
        got = None
        try:
            got = tts_engine.synth(say, engine=str(pack.get("engine") or ""),
                                   voice=str(pack.get("voice") or ""),
                                   rate=str(pack.get("rate") or ""),
                                   pitch=str(pack.get("pitch") or ""),
                                   sapi_voice=str(pack.get("sapi_voice") or ""),
                                   out_path=out_path, budget_s=20.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "合成异常：%s: %s" % (type(exc).__name__, exc),
                    "engines": {e: bool(tts_engine.available(e)) for e in tts_engine.ENGINES}}
        if not got or not os.path.exists(str(got)):
            return {"ok": False, "error": "合成失败（引擎都没起来？）",
                    "engines": {e: bool(tts_engine.available(e)) for e in tts_engine.ENGINES},
                    "engine": str(pack.get("engine") or "")}
        out_path = str(got)
    try:
        engine_used = tts_engine.last_engine()
    except Exception:  # noqa: BLE001
        engine_used = ""
    return {"ok": True, "path": os.path.relpath(out_path, ROOT), "abs": out_path,
            "bytes": os.path.getsize(out_path), "cached": bool(cached),
            "slug": s, "text": say, "engine": engine_used,
            "voice": str(pack.get("voice") or ""), "style": str(pack.get("style") or "")}


def me_names_get() -> dict:
    """她在平台上的名字（寻址用）。唯一来源：``data/persona/me_names.json``。

    为什么单独一条命令：这些名字决定"这条消息是不是在叫她"（群里 @/拍/引用 才算
    叫她，只提名字只降级成 · ），2026-10-27 用户要求**全部由用户手写、不留兜底**，
    所以面板必须能看见它、能改它，否则写错一个字就静默失灵。
    """
    path = os.path.join(DATA, "persona", "me_names.json")
    names: list = []
    try:
        raw = json.loads(open(path, encoding="utf-8").read() or "{}")
        got = raw.get("names") if isinstance(raw, dict) else raw
        if isinstance(got, list):
            names = [str(x).strip() for x in got if str(x).strip()]
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - 文件坏了要能看见，不要静默
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "path": path}
    return {"ok": True, "names": names, "path": path, "empty": not names}


def me_names_set(names, *, dry_run: bool = True, by: str = "panel") -> dict:
    """写 ``me_names.json``。空列表是合法值（= 不认任何名字），不是错误。"""
    if isinstance(names, str):
        raw = [x.strip() for x in names.replace("，", ",").replace("、", ",").split(",")]
    elif isinstance(names, list):
        raw = [str(x).strip() for x in names]
    else:
        return {"ok": False, "error": "names 应该是列表或逗号分隔的字符串"}
    out: list = []
    for one in raw:
        if not one:
            continue
        if len(one) > 20:
            return {"ok": False, "error": "名字太长（%s…）：每个最多 20 字" % one[:8]}
        if one not in out:
            out.append(one)
    if len(out) > 12:
        return {"ok": False, "error": "最多 12 个名字（现在 %d 个）" % len(out)}

    path = os.path.join(DATA, "persona", "me_names.json")
    before = me_names_get()
    if before.get("ok") and before.get("names") == out:
        return {"ok": True, "noop": True, "names": out, "note": "没有变化，不用保存。"}
    if dry_run:
        return {"ok": True, "dry_run": True, "names": out, "before": before.get("names", []),
                "note": "只看不打：确认后她才按这份名单认自己的名字。"}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        _backup(path, "me_names")
    _atomic_write(path, json.dumps({"names": out}, ensure_ascii=False, indent=2))
    try:  # 让 60 秒缓存立刻失效，下一句消息就生效
        from core.identity.self_names import invalidate as _invalidate

        _invalidate()
    except Exception:  # noqa: BLE001 - 缓存清不掉最多等 60 秒
        pass
    _audit_row({"id": _id("me_names"), "at": _now(), "cmd": "me_names_set",
                "target": "data/persona/me_names.json", "undoable": True,
                "summary": "名字 %s -> %s（%s）" % (before.get("names", []), out, by)})
    note = "已写入。她下一句就按这份名单认自己的名字。"
    if not out:
        note = "写成空名单了：群里 @/拍/引用 都认不出来（★ 不会亮），私聊不受影响。"
    return {"ok": True, "names": out, "path": path, "note": note}


def account_list() -> dict:
    """只读盘点 account_registry + 每个号目录/库的实际状态。绝不写库。"""
    import sqlite3

    path = os.environ.get("META_DB_PATH") or os.path.join(DATA, "yinyue_meta.db")
    if not os.path.exists(path):
        return {"ok": False, "error": "元库不在：%s" % os.path.relpath(path, ROOT)}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select account_id, platform, role, data_dir, enabled, config_json "
            "from account_registry order by platform, account_id").fetchall()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    out = []
    for r in rows:
        d = dict(r)
        ddir = str(d.get("data_dir") or "")
        acct_db = os.path.join(ddir, "account.db") if ddir else ""
        probe = {"dir_exists": bool(ddir) and os.path.isdir(ddir),
                 "db_exists": bool(acct_db) and os.path.exists(acct_db),
                 "messages": None, "last_at": None}
        if probe["db_exists"]:
            try:
                c2 = sqlite3.connect("file:%s?mode=ro" % acct_db.replace("\\", "/"),
                                     uri=True, timeout=2.0)
                probe["messages"] = c2.execute("select count(*) from messages").fetchone()[0]
                probe["last_at"] = c2.execute("select max(created_at) from messages").fetchone()[0]
                c2.close()
            except Exception as exc:  # noqa: BLE001
                probe["error"] = "%s: %s" % (type(exc).__name__, exc)
        d["probe"] = probe
        out.append(d)
    # 账号名（2026-10-27）：只用 id 不像"人"。名字在微信/QQ 的 contacts 里
    # （列 wxid / nickname / remark），所以从**同平台已登记账号库**里建索引再套用 ——
    # 主人的名字常常只存在于她（bot）那个账号的 contacts 里（contacts.account_id = bot 的 wxid），
    # 单看自己的库会查不到。绝不写死人名。
    names = {}
    names_at = {}  # 2026-10-27：名字是哪天同步来的——避免把 10-15 的旧昵称当现在
    for d in out:
        _ddir = str(d.get("data_dir") or "")
        _adb = os.path.join(_ddir, "account.db") if _ddir else ""
        if not _adb or not os.path.exists(_adb):
            continue
        try:
            _c = sqlite3.connect("file:%s?mode=ro" % _adb.replace("\\", "/"),
                                 uri=True, timeout=2.0)
            for _w, _nk, _rk, _ua in _c.execute(
                    "select wxid, nickname, remark, updated_at from contacts where is_group = 0"):
                _nm = str(_nk or "").strip() or str(_rk or "").strip()
                if _nm and _w and str(_w) not in names:
                    names[str(_w)] = _nm
                    names_at[str(_w)] = str(_ua or "")
            _c.close()
        except Exception:  # noqa: BLE001 - 名字查不到不算错
            continue
    def _age_text(stamp: str) -> str:
        """把 contacts.updated_at 变成人话：刚刚 / N 小时前 / N 天前。"""
        if not stamp:
            return ""
        try:
            from datetime import datetime as _dt
            _t = _dt.fromisoformat(str(stamp).replace("T", " ")[:19])
            _sec = max(0.0, (_dt.now() - _t).total_seconds())
        except Exception:  # noqa: BLE001
            return ""
        if _sec < 90:
            return "刚刚"
        if _sec < 5400:
            return "%d 分钟前" % int(_sec // 60)
        if _sec < 172800:
            return "%d 小时前" % int(_sec // 3600)
        return "%d 天前" % int(_sec // 86400)

    for d in out:
        d["display_name"] = names.get(str(d["account_id"]), "")
        _at = names_at.get(str(d["account_id"]), "")
        d["display_name_at"] = _at
        d["display_name_age"] = _age_text(_at)
    owners = [d["account_id"] for d in out if str(d.get("role") or "") == "owner"]
    note = ""
    if not out:
        note = "还没有登记任何账号 —— 先跑 scripts/migrate_to_multi_account.py 或手工登记"
    elif not owners:
        note = "没有 role=owner 的账号：她不知道该服务谁"
    else:
        # owner 是**按平台**用的（core/execution/owner_ask.py::_owner_ids(platform)），
        # 所以"微信一个、QQ 一个"是正常形态，同一平台有两个才有歧义。
        per: dict[str, list[str]] = {}
        for d in out:
            if str(d.get("role") or "") == "owner":
                per.setdefault(str(d.get("platform") or "?"), []).append(d["account_id"])
        dup = {k: v for k, v in per.items() if len(v) > 1}
        if dup:
            note = "同一平台有多个 owner（%s）：该平台取哪条不确定，建议每平台一个" % "；".join(
                "%s = %s" % (k, "、".join(v)) for k, v in dup.items())
        elif len(owners) > 1:
            note = "每个平台各一个 owner（%s）—— 正常形态" % "、".join(owners)
    return {"ok": True, "meta": os.path.relpath(path, ROOT), "rows": out,
            "owners": owners, "note": note, "now": _now(), "self_name": self_name()}


# ---- 通道状态（2026-10-27）：登记回答"读哪个库"，这里回答"这个号此刻通不通" ----
NAPCAT_DIR = os.path.join(ROOT, "napcat")
WEBUI = "http://127.0.0.1:6099/webui"


def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    """端口通不通。只做一次 connect，不握手、不发数据。"""
    import socket

    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _proc_names() -> set:
    try:
        import psutil  # type: ignore

        return {str((p.info or {}).get("name") or "").lower()
                for p in psutil.process_iter(["name"])}
    except Exception:  # noqa: BLE001 - 没 psutil 就退化成"看不见进程"
        return set()


def _ws_addr() -> tuple:
    """QQ_WS_URL → (host, port)。默认 ws://127.0.0.1:3001。"""
    import urllib.parse

    raw = (os.environ.get("QQ_WS_URL") or "ws://127.0.0.1:3001").strip()
    try:
        u = urllib.parse.urlparse(raw if "//" in raw else "ws://" + raw)
        return (u.hostname or "127.0.0.1", int(u.port or 3001))
    except Exception:  # noqa: BLE001
        return ("127.0.0.1", 3001)


def _env_has(name: str) -> bool:
    """`.env` 里这个键有没有非空值。**只判空，不读值、不回显**。"""
    import re as _re

    try:
        t = open(os.path.join(ROOT, ".env"), encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    m = _re.search(r"^\s*%s\s*=\s*(\S*)\s*$" % _re.escape(name), t, _re.M)
    return bool(m and m.group(1).strip())


def caps_status(by: str = "panel") -> dict:
    """这一台机器上，各账号"哪些数据真能拿到"（来自 db/wechat_keyring.py 的探测结果）。"""
    import json as _json
    import os as _os

    out = []
    root_ = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    kr_path = _os.path.join(root_, "data", "platforms", "wechat", "keyring.json")
    kr = {}
    try:
        with open(kr_path, encoding="utf-8") as f:
            kr = _json.load(f)
    except Exception:
        kr = {}
    for plat in ("wechat", "qq"):
        base = _os.path.join(root_, "data", "platforms", plat)
        if not _os.path.isdir(base):
            continue
        for acc in sorted(_os.listdir(base)):
            cp = _os.path.join(base, acc, "capabilities.json")
            if not _os.path.isfile(cp):
                continue
            try:
                with open(cp, encoding="utf-8") as f:
                    d = _json.load(f)
            except Exception:
                continue
            caps = d.get("capabilities") or {}
            out.append({"platform": plat, "account": acc,
                        "probed_at": d.get("probed_at", ""),
                        "capabilities": caps,
                        "degraded": d.get("degraded") or [],
                        "ok_labels": [k for k, v in sorted(caps.items()) if v],
                        "bad_labels": d.get("degraded") or []})
    return {"ok": True, "items": out, "keyring_at": kr.get("probed_at", "")}


def channel_get() -> dict:
    """D1 渠道约定：这张表此刻真正生效的值 + 渲染出来她读到的那段话。"""
    try:
        from core.policy import channel as _ch
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "读不到 core/policy/channel.py：%s: %s"
                % (type(exc).__name__, exc)}
    got = {"ok": True, "path": os.path.relpath(CHANNEL, ROOT), "rules": {},
           "factory": {}, "text": {}, "group": {}, "group_text": {}}
    for plat, _lbl in _CHANNEL_PLATFORMS:
        got["rules"][plat] = _ch.effective(plat)
        got["factory"][plat] = _ch.factory(plat)
        got["text"][plat] = _ch.render(_ch.rules(plat))
        # 群里的样子（面板用来显示"群里另加什么"；不额外开开关）
        _sample_group = "%s" % ("x@chatroom" if plat == "wechat" else "group:1")
        got["group"][plat] = _ch.group_extra(plat)
        got["group_text"][plat] = _ch.render(_ch.rules(plat, _sample_group))
    try:
        got["overrides"] = _read_json(CHANNEL, {}) or {}
    except Exception:  # noqa: BLE001
        got["overrides"] = {}
    return got


def channel_status() -> dict:
    """只读：客户端在不在 / 密钥有没有 / WS 通不通。绝不启动、绝不写。"""
    procs = _proc_names()
    host, port = _ws_addr()
    ws_open = _tcp_open(host, port)
    napcat_on = any(("napcat" in n) or ("bootmain" in n) for n in procs)
    wx_on = any(("weixin" in n) or ("wechat" in n) for n in procs)
    key_ok = _env_has("WECHAT_KEY")
    plat = {
        "wechat": {
            "label": "微信",
            "lights": [
                {"k": "客户端", "ok": wx_on, "v": "在跑" if wx_on else "没开/没登录"},
                {"k": "WECHAT_KEY", "ok": key_ok, "v": "有" if key_ok else "缺（读不到消息）"},
            ],
            "hint": "说话靠点按；读消息要密钥",
        },
        "qq": {
            "label": "QQ",
            "lights": [
                {"k": "NapCat", "ok": napcat_on, "v": "在跑" if napcat_on else "没跑"},
                {"k": "WS %d" % port, "ok": ws_open, "v": "通" if ws_open else "不通"},
            ],
            "hint": "扫码后才收消息 · WebUI %s" % WEBUI,
        },
    }
    _qm = _qr_mtime()
    return {"ok": True, "now": _now(), "platforms": plat, "webui": WEBUI,
            "ws": "ws://%s:%d" % (host, port),
            "qq_qr": {"ready": bool(_qm) and not ws_open, "mtime": _qm}}


def _qq_setup_mod():
    """按文件路径装载 scripts/qq_setup.py（项目根不是包，不能直接 import scripts.*）。"""
    import importlib.util
    p = os.path.join(ROOT, "scripts", "qq_setup.py")
    spec = importlib.util.spec_from_file_location("_yinyue_qq_setup", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _napcat_launcher() -> str:
    """官方启动器优先：launcher.bat 会自己去注册表找 QQ.exe 再注入。
    直接跑顶层 napcat.bat（= node.exe ./index.js）在部分机器上会报
    「wrapper.node: The specified module could not be found」，所以只当兜底。"""
    # 2026-10-27：优先 launcher-user.bat —— 它不需要管理员，不会用 wt.exe 提权开窗；
    # launcher.bat 在非管理员时会 `Start-Process wt.exe -Verb runAs`，那个提权终端
    # 我们（非提权进程）按 UIPI 规则根本藏不掉，这就是用户看到的黑框。
    for rel in ("napcat\\launcher-user.bat", "napcat\\launcher-win10-user.bat",
                "napcat\\launcher.bat", "napcat\\launcher-win10.bat", "napcat.bat"):
        p = os.path.join(NAPCAT_DIR, rel)
        if os.path.exists(p):
            return p
    return ""


def _webui_token() -> str:
    try:
        p = os.path.join(NAPCAT_DIR, "napcat", "config", "webui.json")
        return str(json.loads(io.open(p, encoding="utf-8").read()).get("token") or "")
    except Exception:  # noqa: BLE001
        return ""


def _napcat_logtail(n: int = 1200) -> str:
    try:
        p = os.path.join(ROOT, "logs", "napcat.log")
        with io.open(p, encoding="utf-8", errors="replace") as f:
            return f.read()[-n:]
    except Exception:  # noqa: BLE001
        return ""


def _console_windows() -> list:
    """枚举当前可见的顶层窗口，返回 [(hwnd, 进程名, 进程创建时间)]。纯 ctypes。"""
    import ctypes
    from ctypes import wintypes
    import psutil as _ps

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                p = _ps.Process(pid.value)
                out.append((hwnd, (p.name() or "").lower(), p.create_time()))
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        return True

    user32.EnumWindows(_cb, 0)
    return out


def _hide_launcher_windows(since: float, seconds: float = 45.0) -> None:
    """把启动 NapCat 时冒出来的控制台/终端窗口藏起来（不杀进程，只隐藏窗口）。"""
    import ctypes
    import threading
    import time as _t

    targets = {"windowsterminal.exe", "wt.exe", "openconsole.exe", "conhost.exe", "cmd.exe"}

    def _run():
        end = _t.time() + seconds
        hidden = []
        while _t.time() < end:
            try:
                for hwnd, name, ctime in _console_windows():
                    if name in targets and ctime >= since - 3:
                        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
                        hidden.append(hwnd)
            except Exception:  # noqa: BLE001
                pass
            _t.sleep(0.6)
        if hidden:
            log.info("napcat_console_hidden", n=len(hidden))

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def _open_webui() -> dict:
    """在默认浏览器打开 NapCat WebUI（一次性，失败不影响启动）。"""
    try:
        webbrowser.open(WEBUI, new=2)
        return {"ok": True, "url": WEBUI, "token": _webui_token()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": WEBUI, "error": repr(exc)[:120]}


def _qr_file() -> str:
    return os.path.join(NAPCAT_DIR, "napcat", "cache", "qrcode.png")


def _qr_mtime() -> int:
    try:
        p = _qr_file()
        return int(os.path.getmtime(p)) if os.path.exists(p) else 0
    except OSError:
        return 0


def napcat_start(*, by: str = "console") -> dict:
    if _tcp_open("127.0.0.1", 6099) or _tcp_open("127.0.0.1", 3001):
        return {"ok": True, "url": WEBUI, "note": "NapCat 已经在跑了，没重复启动",
                "webui_token": _webui_token()}
    # 首选：项目自带的无窗口注入（scripts/qq_setup.start_napcat_detached）。
    # 直接跑 NapCatWinBootMain.exe（CREATE_NO_WINDOW），不起 cmd、不提权、不开浏览器。
    _detached_note = ""
    try:
        _qs = _qq_setup_mod()
        _ok0, _note0 = _qs.start_napcat_detached(NAPCAT_DIR, open_webui=False)
        _detached_note = _note0
        if not _ok0:
            log.warning("napcat_detached_failed", note=_note0[:200])
    except Exception as exc:  # noqa: BLE001
        log.warning("napcat_detached_error", err=repr(exc)[:200])

    _t0 = time.time()
    if _detached_note and "已无窗口拉起" in _detached_note:
        for _ in range(60):  # 最多 30 秒等 WebUI 起来 + 二维码落盘
            if _tcp_open("127.0.0.1", 6099, timeout=0.4):
                for _ in range(40):
                    if _qr_mtime() >= int(_t0) - 90:
                        return {"ok": True, "url": WEBUI, "webui_token": _webui_token(),
                                "qr_ready": True, "qr_mtime": _qr_mtime(), "no_window": True,
                                "note": "NapCat 起来了（无窗口），二维码在下面 —— 手机 QQ 扫它",
                                "webui": {"url": WEBUI, "token": _webui_token(),
                                          "note": "没自动弹浏览器：二维码同一张，就在卡片里"}}
                    time.sleep(0.5)
                break
            time.sleep(0.5)

    bat = _napcat_launcher()
    if not bat:
        return {"ok": False, "error": "没找到 NapCat 启动器：先跑 平台接入.bat 选 qq 装一遍"}
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    _t_launch = time.time()
    logf = io.open(os.path.join(ROOT, "logs", "napcat.log"), "ab")
    try:
        subprocess.Popen(["cmd", "/c", os.path.basename(bat)], cwd=os.path.dirname(bat),
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=0x00000008 | 0x00000200)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动失败：{exc!r}"}
    finally:
        logf.close()
    _hide_launcher_windows(_t_launch)  # 把 launcher 的控制台窗口藏掉
    _t0 = time.time()
    for _ in range(25):
        time.sleep(1)
        if _tcp_open("127.0.0.1", 6099, timeout=0.4):
            for _ in range(40):  # 再等最多 20 秒，让二维码落盘
                if _qr_mtime() >= int(_t0) - 60:
                    return {"ok": True, "url": WEBUI, "webui_token": _webui_token(),
                            "qr_ready": True, "qr_mtime": _qr_mtime(),
                            "note": "NapCat 起来了，登录二维码已显示在下面 —— 手机 QQ 扫它",
                            "webui": {"url": WEBUI, "token": _webui_token(),
                                      "note": "没自动弹浏览器：二维码同一张，就在卡片里"}}
                time.sleep(0.5)
            return {"ok": True, "url": WEBUI, "webui_token": _webui_token(),
                    "qr_ready": False, "qr_mtime": _qr_mtime(),
                    "note": "NapCat 起来了（WebUI 已开），但二维码还没生成 —— 稍等或点「刷新二维码」"}
    return {"ok": False, "error": "起了但 25 秒内 WebUI 6099 没开",
            "log": _napcat_logtail(), "webui_token": _webui_token()}


def napcat_qr(*, by: str = "console") -> dict:
    r = napcat_start(by=by)
    if not r.get("ok"):
        return r
    hit = _napcat_logtail(3000)
    qi = "需要人工确认" if "需要人工确认" in hit else ""
    return {"ok": True, "url": WEBUI, "webui_token": r.get("webui_token") or _webui_token(),
            "note": "WebUI 是调试入口（看日志 / 改端口）；扫码请直接用面板里的二维码" + ("（" + qi + "）" if qi else "")}


def wechat_key_hook(*, by: str = "panel") -> dict:
    """打开 DbkeyHookUI.exe（项目里自带的那把钥匙）。它负责抓 key 并写 .env。"""
    import subprocess

    cands = []
    for dp, dn, fn in os.walk(ROOT):
        if any(x in dp for x in (".git", ".venv", "__pycache__", "napcat",
                                 "node_modules", "backups")):
            continue
        for f in fn:
            if f.lower().startswith("dbkeyhook") and f.lower().endswith(".exe"):
                cands.append(os.path.join(dp, f))
        if len(cands) > 2:
            break
    if not cands:
        return {"ok": False,
                "error": "没找到 DbkeyHookUI.exe —— 抓 key 需要那个工具，项目里没有装"}
    exe = cands[0]
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe),
                         creationflags=0x00000008 if os.name == "nt" else 0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "启动 %s 失败：%s" % (os.path.basename(exe), exc)}
    return {"ok": True,
            "note": "已打开 %s：点「获取密钥」→ 把 64 位值写回 .env 的 WECHAT_KEY → 重启引擎"
                    % os.path.basename(exe)}


# ---- 账号登记的新增/改/删（2026-10-27）：委托给 scripts/account_admin.py ------
ADMIN = os.path.join(ROOT, "scripts", "account_admin.py")
ADMIN_PY = PYW  # 用它自己的解释器（有 sqlalchemy）


def _admin(*argv, timeout: float = 90.0) -> dict:
    """跑 account_admin.py，把最后一行 JSON 原样返回。

    为什么走子进程：写生产库要先 `db.write_guard.declare_writes_allowed()`（声明
    "本进程可写生产库"），而"声明"是**入口**该做的事。采集器不该为了改一行账号
    变成"可写生产库的进程"；放子进程里，声明随子进程消失，采集器始终只读。
    """
    import subprocess

    try:
        p = subprocess.run([ADMIN_PY, ADMIN] + [str(a) for a in argv],
                           cwd=ROOT, capture_output=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "调不起 account_admin.py：%s: %s" % (type(exc).__name__, exc)}
    out = (p.stdout or b"").decode("utf-8", "replace")
    for line in reversed([l.strip() for l in out.splitlines() if l.strip().startswith("{")]):
        try:
            return json.loads(line)
        except Exception:  # noqa: BLE001
            continue
    err = (p.stderr or b"").decode("utf-8", "replace")[-400:]
    return {"ok": False, "error": "account_admin.py 没回 JSON（rc=%s）：%s" % (p.returncode, err or out[-300:])}


def account_probe(args: dict) -> dict:
    args = args or {}
    return _admin("probe", "--platform", args.get("platform", ""),
                  "--account-id", args.get("account_id", ""),
                  "--data-dir", args.get("data_dir", ""),
                  "--role", args.get("role", ""))


def account_save(args: dict, *, confirm: bool = False, by: str = "panel") -> dict:
    args = args or {}
    argv = ["save", "--platform", args.get("platform", ""),
            "--account-id", args.get("account_id", ""),
            "--data-dir", args.get("data_dir", ""),
            "--role", args.get("role", ""),
            "--enabled", "1" if args.get("enabled", 1) else "0"]
    if not confirm:
        got = _admin(*argv)
        got["dry_run"] = True
        got["note"] = "只看不打（你还没确认）；确认后才真的写"
        return got
    out = _admin(*(argv + ["--allow-production"]))
    try:
        _audit_row({
            "id": hashlib.sha1(("%s|%s|%s" % (_now(), by, args.get("account_id"))).encode()).hexdigest()[:10],
            "at": _now(), "cmd": "account_save", "undoable": False,
            "summary": "%s 写账号 %s/%s role=%s enabled=%s → %s" % (
                by, args.get("platform"), args.get("account_id"), args.get("role"),
                args.get("enabled", 1), out.get("action") or out.get("error")),
        })
    except Exception:  # noqa: BLE001  审计失败不影响主流程
        pass
    return out


def account_remove(args: dict, *, confirm: bool = False, by: str = "panel") -> dict:
    account_id = str((args or {}).get("account_id") or "")
    if not confirm:
        return {"ok": False, "dry_run": True,
                "error": "移除要确认（面板会再问一次）：%s" % account_id}
    out = _admin("remove", "--account-id", account_id, "--allow-production")
    try:
        _audit_row({
            "id": hashlib.sha1(("%s|%s|remove|%s" % (_now(), by, account_id)).encode()).hexdigest()[:10],
            "at": _now(), "cmd": "account_remove", "undoable": False,
            "summary": "%s 从登记表移除 %s → %s" % (by, account_id, "ok" if out.get("ok") else out.get("error")),
        })
    except Exception:  # noqa: BLE001
        pass
    return out


# ---- 2026-10-27：card_show / card_save 支持 .md 整块卡（覆盖上面的 json 版）----
_card_show_json = card_show
_card_save_json = card_save


def _card_show_md(nm: str) -> dict:
    path = _md_path(nm)
    try:
        meta, body = _md_split(open(path, encoding="utf-8").read())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "这张卡读不出来：%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "name": nm, "card_name": str(meta.get("name") or nm),
            "active": nm == active_card_name(), "kind": "md",
            "bytes": os.path.getsize(path), "path": os.path.relpath(path, ROOT),
            "text": body, "chars": len(body), "cap": _MD_CAP,
            "voice": str(meta.get("voice") or ""), "looks": str(meta.get("looks") or "")}


def _card_save_md(nm: str, patch: dict, *, dry_run: bool = True, by: str = "panel") -> dict:
    # 写回契约（换行/尾空行/上限/素材体检）只在 core/identity/cardfile.py 定义
    from core.identity import cardfile as _cardfile
    path = _md_path(nm)
    before_text = open(path, encoding="utf-8").read()
    meta, body = _md_split(before_text)
    if "text" in patch:
        body = _cardfile.normalise(patch.get("text"))
    for k in _MD_HEADER:
        if k in patch:
            meta[k] = str(patch.get(k) or "").strip()
    if not body:
        return {"ok": False, "error": "卡不能是空的（整张卡就是一块提示词，空了她就没人格）。"}
    _rep = _cardfile.check(body)
    if _rep["errors"]:
        return {"ok": False, "error": "；".join(_rep["errors"]), "check": _rep}
    _found = _cardfile.assets(meta.get("looks"))
    if _found["blockers"]:
        return {"ok": False, "error": "素材许可不允许出包：%s" % "；".join(
            "%s(%s)" % (b.get("kind"), b.get("reason")) for b in _found["blockers"]),
            "assets": _found}
    after_text = _cardfile.join(meta, body)
    if after_text.rstrip() == before_text.rstrip():
        return {"ok": True, "dry_run": dry_run, "noop": True, "name": nm,
                "chars": len(body), "note": "一个字都没改。"}
    diff = _diff(before_text, after_text, nm + ".md")
    summary = "改整块卡 %s（%d 字）" % (nm, len(body))
    if dry_run:
        return {"ok": True, "dry_run": True, "diff": diff, "name": nm,
                "chars": len(body), "cap": _MD_CAP, "summary": summary}
    backup = _backup(path, "card")
    _atomic_write(path, after_text)
    active = nm == active_card_name()
    row = {"id": _id(), "at": _now(), "by": by, "cmd": "card_save", "target": "persona",
           "path": os.path.relpath(path, ROOT), "summary": summary,
           "changed": ["text"], "before_sha": _sha(before_text),
           "after_sha": _sha(after_text), "backup": os.path.relpath(backup, ROOT),
           "diff": diff[:MAX_AUDIT_DIFF]}
    _audit_row(row)
    return {"ok": True, "dry_run": False, "diff": diff, "name": nm, "audit_id": row["id"],
            "chars": len(body), "cap": _MD_CAP, "summary": summary, "active": active,
            "note": ("她这张就是生效卡，下一拍就按新写法说话。" if active else
                     "这不是生效卡，改完不影响她说话；要她用这张，点「切到这张卡」。")}


def card_show(name: str = "") -> dict:  # noqa: F811 - 有意覆盖：先看 .md，再退回 json
    """打开一张卡：.md 卡回整块正文（text），json 卡回字段表。"""
    nm = (name or active_card_name() or "").strip()
    if nm.endswith(".md"):
        nm = nm[: -len(".md")]
    if nm and os.path.exists(_md_path(nm)):
        return _card_show_md(nm)
    return _card_show_json(name)


def card_save(name: str, patch: dict, *, dry_run: bool = True, by: str = "panel") -> dict:  # noqa: F811
    """改一张卡：.md 卡写整块正文（patch["text"]），json 卡写字段。"""
    nm = (name or active_card_name() or "").strip()
    if nm.endswith(".md"):
        nm = nm[: -len(".md")]
    if nm and os.path.exists(_md_path(nm)):
        return _card_save_md(nm, patch or {}, dry_run=dry_run, by=by)
    return _card_save_json(name, patch, dry_run=dry_run, by=by)


# 2026-11-01：这几条执行了就没法「再看一眼」——关服务、重启引擎、删图、删账号。
# 以前它们完全无视 dry_run，面板传 dry_run:true 也照停；而 dry_run 默认值又是 true，
# 于是「预演」这个词在这些命令上是纯装饰。现在必须由调用方显式写 dry_run=false
# 或 confirm=true（严格判 is False，不吃默认值）。
_NEED_CONFIRM = {"svc_start", "svc_stop", "svc_restart", "restart_engine",
                 "looks_delete", "account_remove"}


#: 被挡下来的原因 → 人话（面板只显示这一句，别在 JS 里再写一份解释）。
#: 2026-11-05 §45：词表搬到 `core.observability.blocked_log` —— 周报要用同一套词，
#: 两份词表迟早对不上（汇总里叫一个名、面板上叫另一个名，就没人信这张表了）。
from core.observability import blocked_log as _blocked_log  # noqa: E402 - 放在使用处附近

_BLOCK_LABELS = _blocked_log.BLOCK_LABELS


def _block_label(why: str) -> str:
    """把 ``why`` 翻成人话：``content:*`` / ``spont:*`` 带参数，其余查表。

    词表在 `core.observability.blocked_log`（周报用的是同一份，见 §45）。
    """
    try:
        return _blocked_log.label(why)
    except Exception:  # noqa: BLE001 - 面板文案不许把面板带崩
        return str(why or "（没写原因）")


def _blocked_name(chat_id: str) -> str:
    """这个会话叫什么（只给面板看，不参与任何判定）。认不出回空串。"""
    try:
        from loop import ports as _ports

        return str(_ports.label_for(str(chat_id or "")) or "")
    except Exception:  # noqa: BLE001 - 名字只是给人看的，读不到就不写
        return ""


def speak_policy_blocked(limit: int = 12) -> dict:
    """最近被挡下来的话（只读）。

    2026-11-05 §40.6 第一条：以前只有白名单往 `logs/blocked.jsonl` 里记 ——
    内容闸（钱 / 地址 / 替人答应 / 时间承诺）、去重、自发闸拦下来的话谁都不知道。
    现在四道闸共用这一个旁证文件，这里按 `why` 翻成人话再交给面板。

    为什么不写 `outbound_queue`：那不是"要发的东西"，写进去会把回读核对、
    重发、周报的凭据率全算错（E-91 的教训）。
    """
    path = os.path.join(ROOT, "logs", "blocked.jsonl")
    items: list[dict] = []
    n_bad = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except ValueError:
                    n_bad += 1
    except FileNotFoundError:
        return {"ok": True, "items": [], "path": path, "note": "还没有话被挡下来"}
    except OSError as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "path": path}
    by_why: dict = {}
    for it in items:
        why = str(it.get("why") or "")
        by_why[why] = by_why.get(why, 0) + 1
        it["label"] = _block_label(why)
        if not it.get("name"):
            it["name"] = _blocked_name(str(it.get("chat_id") or ""))
    by_label: dict = {}
    for it in items:
        _lb = str(it.get("label") or "")
        by_label[_lb] = by_label.get(_lb, 0) + 1
    out = {"ok": True, "items": items[-int(limit or 12):], "path": path, "by_label": by_label,
           "total": len(items), "by_why": by_why}
    if n_bad:
        out["bad_lines"] = n_bad
    return out
def speak_policy_blocked_days(days: int = 7) -> dict:
    """最近 N 天被挡下来的话：按天 + 按哪道闸（§45 第二条）。

    面板上只列最近 8 条，想翻「这一周到底被挡了多少、是哪道闸挡的」得靠这个。
    只读 `logs/blocked.jsonl`；文件不存在时 `missing` 为真（面板要说「还没记过」，
    不许说「一次都没拦过」——那两件事的结论相反）。
    """
    path = os.path.join(ROOT, "logs", "blocked.jsonl")
    got = _blocked_log.summarize(days=int(days or 7), path=path)
    top = sorted(dict(got.get("by_label") or {}).items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    got["top"] = [{"label": k, "n": v} for k, v in top]
    return got


def speak_policy_add(chat_id: str = "", name: str = "", proactive: bool = False,
                     platform: str = "", by: str = "panel") -> dict:
    """把一（群）会话加进说话白名单 —— 面板「最近被挡下来的」那一行上的按钮走这里。

    纪律（照 §38 的教训）：
    - **认不出归属就不加**（`_account_of` 空 → 回 error）：白名单是"哪个账号的哪些会话"，
      不知道账号就不知道该写到哪里，瞎写等于让白名单形同虚设（E-69）。
    - 已存在的条目**不降级**：原来标了 ``proactive`` 的，这次不加参数也还是 ``proactive``。
    - 写之前留一份带时间戳的 `.bak-`（与面板保存同一条路），写之后清缓存 → 下一拍生效。
    """
    from loop import speak_policy as sp

    cid = str(chat_id or "").strip()
    if not cid:
        return {"ok": False, "error": "chat_id 不能为空"}
    plat = str(platform or "") or sp.platform_of(cid)
    aid = sp._account_of(plat, cid)
    if not aid:
        return {"ok": False, "chat_id": cid, "platform": plat,
                "error": "认不出这个会话属于哪个账号（先让采集器同步一次，或直接去「说话白名单」里加）"}
    data = sp.load()
    box = (data.setdefault("platforms", {}).setdefault(plat, {"default": "allowlist", "accounts": {}})
           .setdefault("accounts", {}).setdefault(aid, {"mode": "allowlist", "chats": {}}))
    chats = box.setdefault("chats", {})
    prev = chats.get(cid) or {}
    ent = {"name": str(name or prev.get("name") or ""), "at": _now(), "by": by}
    if proactive or prev.get("proactive"):
        ent["proactive"] = True
    chats[cid] = ent
    _bak_ok = True
    try:
        bak = sp.PATH.with_suffix(".json.bak-" + time.strftime("%Y%m%d-%H%M%S"))
        if sp.PATH.exists():
            bak.write_text(sp.PATH.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        # 备份失败不该拦下"加白名单"这件事，但**不许静默**：回执里写出来，面板能看见。
        _bak_ok = False
    sp.save(data)
    _audit_row({"at": _now(), "kind": "cmd", "path": "data/persona/speak_policy.json",
                "summary": "面板一键加白名单：%s" % cid, "by": by, "ok": True})
    return {"ok": True, "chat_id": cid, "account": aid, "platform": plat,
            "name": ent["name"], "n": len(chats), "backup": _bak_ok,
            "note": "已加进白名单：%s（只回话，不主动）" % (ent["name"] or cid)}


def apply(body: dict) -> dict:
    cmd = str((body or {}).get("cmd") or "")
    args = dict((body or {}).get("args") or {})
    # 面板有时把参数写在 body 顶层（{cmd, name, ...}）而不是 args 里：兼容两种写法，
    # 但 cmd/args/dry_run/by 是"命令行"，不并进参数。
    for _k, _v in (body or {}).items():
        if _k in ("cmd", "args", "dry_run", "by"):
            continue
        args.setdefault(_k, _v)
    dry = bool((body or {}).get("dry_run", True))
    by = str((body or {}).get("by") or "panel")[:24]
    if cmd in _NEED_CONFIRM and not (
            (body or {}).get("dry_run") is False
            or (body or {}).get("confirm") is True
            or args.get("confirm") is True):
        return {"ok": False, "need_confirm": True, "cmd": cmd,
                "error": "%s 会改状态且无法预演，必须显式 dry_run=false 或 confirm=true" % cmd}
    try:
        if cmd == "set":
            return set_values(str(args.get("target") or ""), args.get("patch") or {},
                              dry_run=dry, by=by)
        if cmd == "rollback":
            if dry:
                return {"ok": False, "error": "回滚不支持预演"}
            return rollback(str(args.get("audit_id") or ""), by=by)
        if cmd == "looks_status":
            return looks_status(str(args.get("slug") or ""))
        if cmd == "looks_upload":
            # 这不是"改设定"，是收文件：自己带备份、自己记审计，不走 dry_run 闸
            return looks_upload(args.get("files") or [], slug=str(args.get("slug") or ""), by=by)
        if cmd == "looks_generate":
            return looks_generate(slug=str(args.get("slug") or ""), by=by,
                                  limit=int(args.get("limit") or 0))
        if cmd == "looks_setcanon":
            return looks_setcanon(text=str(args.get("text") or ""), slug=str(args.get("slug") or ""), by=by)
        if cmd == "looks_delete":
            return looks_delete(str(args.get("file") or ""), slug=str(args.get("slug") or ""), by=by)
        if cmd == "speak_policy_proactive":
            return speak_policy_proactive(args.get("accounts") or [], by=by)
        if cmd == "speak_policy_get":
            return speak_policy_get(days=int(args.get("days") or 30))
        if cmd == "speak_policy_blocked":
            return speak_policy_blocked(limit=int(args.get("limit") or 12))
        if cmd == "speak_policy_blocked_days":
            return speak_policy_blocked_days(days=int(args.get("days") or 7))
        if cmd == "speak_policy_add":
            return speak_policy_add(chat_id=str(args.get("chat_id") or ""),
                                    name=str(args.get("name") or ""),
                                    proactive=bool(args.get("proactive")),
                                    platform=str(args.get("platform") or ""), by=by)
        if cmd == "speak_policy_search":
            return speak_policy_search(q=str(args.get("q") or ""),
                                       limit=int(args.get("limit") or 20))
        if cmd == "speak_policy_seed":
            return speak_policy_seed(days=int(args.get("days") or 30), by=by)
        if cmd == "speak_policy_set":
            try:
                _exp = float(args.get("expect_mtime") or 0.0)
            except (TypeError, ValueError):
                _exp = 0.0
            return speak_policy_set(accounts=args.get("accounts"), by=by, expect_mtime=_exp)
        if cmd == "restart_engine":
            return restart_engine(by=by)
        if cmd == "state":
            return state()
        if cmd == "svc_status":
            return svc_status()
        if cmd == "svc_start":
            return svc_start(by=by)
        if cmd == "svc_stop":
            return svc_stop(by=by)
        if cmd == "svc_restart":
            return svc_restart(by=by)
        if cmd == "me_names_get":
            return me_names_get()
        if cmd == "me_names_set":
            return me_names_set(args.get("names"), dry_run=dry, by=by)
        if cmd == "account_list":
            return account_list()
        if cmd == "account_probe":
            return account_probe(args)
        if cmd == "account_save":
            return account_save(args, confirm=bool(args.get("confirm")), by=by)
        if cmd == "account_remove":
            return account_remove(args, confirm=bool(args.get("confirm")), by=by)
        if cmd == "caps_status":
            return caps_status(by=by)
        if cmd == "channel_get":
            return channel_get()
        if cmd == "sticker_register":
            return sticker_register(str(args.get("md5") or ""), str(args.get("key") or ""), str(args.get("label") or ""), platform=str(args.get("platform") or ""), by=by)
        if cmd == "sticker_forget":
            return sticker_forget(str(args.get("md5") or ""), platform=str(args.get("platform") or ""), by=by)
        if cmd == "sticker_registered":
            return sticker_registered(args.get("platform") or "", args.get("account_id") or "")
        if cmd == "sticker_pool":
            return sticker_pool(str(args.get("platform") or ""), str(args.get("account_id") or ""),
                                int(args.get("limit") or 5000))
        if cmd == "diagnose_dump":
            return diagnose_dump(note=str(args.get("note") or ""), by=by)
        if cmd == "channel_status":
            return channel_status()
        if cmd == "napcat_start":
            return napcat_start(by=by)
        if cmd == "napcat_qr":
            return napcat_qr(by=by)
        if cmd == "wechat_key_hook":
            return wechat_key_hook(by=by)
        if cmd == "card_list":
            return card_list()
        if cmd == "card_show":
            return card_show(str(args.get("name") or ""))
        if cmd == "card_save":
            return card_save(str(args.get("name") or ""), args.get("patch") or {},
                             dry_run=dry, by=by)
        if cmd == "card_switch":
            return card_switch(str(args.get("name") or ""),
                               dry_run=not bool(args.get("confirm")), by=by)
        if cmd == "card_new":
            return card_new(str(args.get("name") or ""), source=str(args.get("source") or ""),
                            overwrite=bool(args.get("confirm")), by=by)
        if cmd == "voice_info":
            return voice_info(str(args.get("slug") or ""))
        if cmd == "voice_preview":
            return voice_preview(str(args.get("slug") or ""), str(args.get("text") or ""))
        return {"ok": False, "error": "不认识的命令：%r" % cmd}
    except Exception as exc:  # noqa: BLE001  —— 绝不能把异常抛回给面板
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
