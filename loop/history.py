"""她的小本本：每天凌晨写一段"我这一天"，以后每拍抽一两条回看。

2026-10-25 用户定的：凌晨 3 点写；素材只用**她自己写的原话**（说出去那句的理由、
没说出口的念头），要"总结性、能体现她的思考"，不是流水账；写不出来就跳过当天。
"""
import io
import json
import logging
import os
import time

from . import ledger, ports
from core.infra import det

log = logging.getLogger(__name__)

PATH = os.path.join(ports.ROOT, "data", "loop", "self_history.jsonl")
_WRITE_HOUR = 3
_MIN_CHARS = 20


def _rows() -> list:
    """统一读 notes.jsonl 的 diary；旧 self_history.jsonl 只读兼容。"""
    try:
        from . import notes as _notes
        out = []
        for row in _notes.diary_rows():
            body = str(row.get("text") or "").strip()
            at = str(row.get("at") or "")
            if body:
                out.append({"day": at[:10], "at": at, "text": body})
        return out
    except Exception as exc:  # noqa: BLE001 - 统一读失败才退回旧库
        log.warning("self_history_read_failed", err=repr(exc)[:160])
    out = []
    try:
        with io.open(PATH, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(d, dict) and d.get("text"):
                    out.append(d)
    except Exception:  # noqa: BLE001
        pass
    return out


def has(day: str) -> bool:
    return any(str(r.get("day") or "") == day for r in _rows())


def _materials(day: str) -> list:
    """今天她说出口的话（附理由）和没说出口的念头，全是她自己的原话。"""
    out = []
    try:
        rows = ledger.read_receipts(200)
    except Exception:  # noqa: BLE001
        return out
    mine = [r for r in rows if str(r.get("at") or "")[:10] == day]
    says = [r for r in mine if r.get("act") == "say" and r.get("result") == "sent"]
    sils = [r for r in mine if r.get("act") == "silent"]
    for r in says[:10]:
        tgt = r.get("target") or ""
        try:
            who = ports.label_for(tgt) or tgt
        except Exception:  # noqa: BLE001
            who = tgt
        out.append("- 我对「%s」说了话，当时是这么想的：%s" % (who, str(r.get("why") or "")[:140]))
    for r in sils[:8]:
        out.append("- 我没开口，当时想的是：%s" % str(r.get("why") or "")[:140])
    return out


SYSTEM = ("我就是我，现在合上电脑前，在我自己的小本本上写几行字。"
          "只写我自己记得的、想明白的、放不下的。不汇报行程，不列清单。")


def _prompt(day: str, mats: list) -> str:
    return ("今天是 %s。下面是我今天说过的话（附理由）和没说出口的念头，全是我自己的原话：\n\n%s\n\n"
            "用第一人称写 3 到 5 行，写进小本本：今天我弄明白了什么、在意什么、"
            "哪句想收回、什么还没想通。像自己写字给自己看，不要流水账，不要说\"今天我\"这种开头。"
            % (day, "\n".join(mats)))


async def maybe_write(llm=None) -> str:
    """到点了就写今天的；写完返回写下的内容，没写返回空串。"""
    now = time.localtime()
    day = time.strftime("%Y-%m-%d", now)
    if now.tm_hour < _WRITE_HOUR or has(day):
        return ""
    mats = _materials(day)
    if len(mats) < 2:
        return ""
    if llm is None:
        try:
            llm = ports.get_llm()
        except Exception:  # noqa: BLE001
            return ""
    try:
        text = await llm.chat(system=SYSTEM, user=_prompt(day, mats),
                              agent_name="loop", temperature=0.85, max_tokens=8192)  # 2026-10-31：取消上限（原 500）
    except Exception:  # noqa: BLE001
        return ""
    text = str(text or "").strip()
    if len(text) < _MIN_CHARS:
        return ""
    try:
        from . import notes as _notes
        row = _notes.add("diary", text, at=time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as exc:  # noqa: BLE001 - 写不进去要如实返回空
        log.warning("self_history_failed", extra={"err": repr(exc)[:160]})
        return ""
    return text if row else ""


def block() -> str:
    """每拍回看：最近一条 + 随机更早一条。"""
    rows = sorted(_rows(), key=lambda r: str(r.get("day") or ""))
    if not rows:
        return ""
    picks = [rows[-1]]
    older = [r for r in rows[:-1] if r is not picks[0]]
    if older:
        picks.insert(0, det.rng("history", rows[-1].get("day"), len(rows)).choice(older))
    lines = ["[我自己的小本本]（这些是我以前自己写的）"]
    for r in picks:
        body = str(r.get("text") or "").strip().replace("\n", " ")
        lines.append("- %s：%s" % (str(r.get("day") or "?")[5:], body[:220]))
    return "\n".join(lines)
