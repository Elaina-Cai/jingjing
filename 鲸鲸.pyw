# -*- coding: utf-8 -*-
"""鲸鲸（桌面窗口版）。

2026-10-26：web 端下线后重做的原生外壳。**不含任何网页**：
窗口只看本地文件（账本 / 心跳 / 引擎心跳），按钮只调用本目录的 .bat 和哨兵文件。

开着的窗口每 2 秒刷新一次：她在不在、这一拍在干什么、最近说了什么。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
LEDGER_DIR = ROOT / "data" / "loop"
CREATE_NO_WINDOW = 0x08000000

C_BG = "#11161d"
C_FG = "#d8e2ea"
C_DIM = "#8593a3"
C_OK = "#48d597"
C_WARN = "#e0c060"
C_BAD = "#e06c75"


def _read_json(path: Path, max_age: float, *keys_ts: str) -> tuple[dict, str]:
    """读一个心跳文件，返回 (数据, 状态)。状态：ok / stale / missing。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, "missing"
    stamp = 0.0
    for k in ("at", "ts", "last_tick_at") + keys_ts:
        v = data.get(k)
        if isinstance(v, (int, float)) and v > 1e9:
            stamp = max(stamp, float(v))
        elif isinstance(v, str) and v:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    stamp = max(stamp, time.mktime(time.strptime(v[:19], fmt)))
                    break
                except Exception:
                    continue
    if stamp and now_ts() - stamp > max_age:
        return data, "stale"
    return data, "ok"


def now_ts() -> float:
    return time.time()


def _ledger_today() -> Path:
    return LEDGER_DIR / ("ledger-%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))


def _recent_says(limit: int = 14) -> list[dict]:
    out: list[dict] = []
    for path in (_ledger_today(),):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if str(row.get("act") or "") == "say" and str(row.get("text") or "").strip():
                out.append(row)
                if len(out) >= limit:
                    break
    return out


def _spawn(args: list[str]) -> None:
    subprocess.Popen(args, cwd=str(ROOT), creationflags=CREATE_NO_WINDOW,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class App:
    def __init__(self, master: tk.Tk) -> None:
        self.master = master
        master.title("鲸鲸")
        master.geometry("760x520")
        master.minsize(560, 380)
        master.configure(bg=C_BG)

        head = tk.Frame(master, bg=C_BG)
        head.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(head, text="鲸鲸", bg=C_BG, fg=C_FG,
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        self.lbl_clock = tk.Label(head, text="", bg=C_BG, fg=C_DIM,
                                  font=("Consolas", 10))
        self.lbl_clock.pack(side="right")

        self.status = {}
        box = tk.Frame(master, bg=C_BG)
        box.pack(fill="x", padx=14, pady=4)
        for key, text in (("engine", "引擎（说话）"), ("main", "主程序（同步/记忆）")):
            row = tk.Frame(box, bg=C_BG)
            row.pack(fill="x", pady=1)
            dot = tk.Label(row, text="●", bg=C_BG, fg=C_DIM, font=("Segoe UI", 11))
            dot.pack(side="left")
            lbl = tk.Label(row, text=text, bg=C_BG, fg=C_FG, width=20, anchor="w",
                           font=("Microsoft YaHei UI", 10))
            lbl.pack(side="left")
            val = tk.Label(row, text="—", bg=C_BG, fg=C_DIM, anchor="w",
                           font=("Consolas", 9))
            val.pack(side="left", fill="x", expand=True)
            self.status[key] = (dot, val)

        tk.Frame(master, bg="#243040", height=1).pack(fill="x", padx=14, pady=8)
        tk.Label(master, text="最近开口（新的在上面）", bg=C_BG, fg=C_DIM, anchor="w",
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", padx=14)
        wrap = tk.Frame(master, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=(2, 8))
        self.text = tk.Text(wrap, bg="#0d1218", fg=C_FG, relief="flat", wrap="word",
                            font=("Microsoft YaHei UI", 10), padx=8, pady=6,
                            insertbackground=C_FG)
        bar = tk.Scrollbar(wrap, command=self.text.yview, bg=C_BG, troughcolor=C_BG,
                           highlightthickness=0, borderwidth=0)
        self.text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("d", foreground=C_DIM)
        self.text.tag_configure("t", foreground=C_FG)
        self.text.tag_configure("w", foreground="#9aa8b8", font=("Microsoft YaHei UI", 9, "italic"))
        self.text.configure(state="disabled")

        btns = tk.Frame(master, bg=C_BG)
        btns.pack(fill="x", padx=14, pady=(0, 12))
        for text, cmd in (("启动", self.start), ("重启", self.restart), ("停止", self.stop),
                          ("账本文件夹", self.open_ledger), ("日志", self.open_logs),
                          ("关机", self.quit)):
            b = tk.Button(btns, text=text, command=cmd, bg="#1b2530", fg=C_FG,
                          activebackground="#2a3a4a", activeforeground=C_FG,
                          relief="flat", padx=12, pady=5, bd=0,
                          font=("Microsoft YaHei UI", 10))
            b.pack(side="left", padx=(0, 8))

        self.refresh()

    # ---------- 刷新 ----------
    def refresh(self) -> None:
        self.lbl_clock.configure(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        try:
            self._refresh_status()
            self._refresh_says()
        except Exception as exc:  # noqa: BLE001 - 这个窗口永远不能因为读文件崩掉
            self._set("engine", "?", "窗口自身出错：%r" % (exc,), C_BAD)
        self.master.after(2000, self.refresh)

    def _set(self, key: str, dot: str, text: str, color: str) -> None:
        d, v = self.status[key]
        d.configure(fg=color)
        v.configure(text=("%s %s" % (dot, text)).strip(), fg=C_DIM if color == C_DIM else C_FG)

    def _refresh_status(self) -> None:
        engine, e_state = _read_json(LOGS / "loop_engine.json", 90.0)
        if e_state == "missing":
            self._set("engine", "没在跑", "logs/loop_engine.json 不存在（点「启动」）", C_BAD)
        elif e_state == "stale":
            self._set("engine", "停了", "最后心跳 %s（超过 90 秒没动）" % engine.get("at", "?"), C_BAD)
        else:
            self._set("engine", "在跑", "pid %s · 第 %s 拍 · %s%s" % (
                engine.get("pid"), engine.get("ticks"), engine.get("last_tick_at", "?"),
                " · 静着" if engine.get("idle") else ""), C_OK)

        hb, h_state = _read_json(LOGS / "loop_heartbeat.json", 60.0)
        if h_state == "missing":
            self._set("main", "没在跑", "logs/loop_heartbeat.json 不存在", C_BAD)
        elif h_state == "stale":
            self._set("main", "停了", "最后心跳 %s" % hb.get("ts", "?"), C_WARN)
        else:
            self._set("main", "在跑", "pid %s · 拍数 %s · 任务 %s" % (
                hb.get("pid"), hb.get("main_loop_ticks", "?"), hb.get("job") or "空闲"), C_OK)

    def _refresh_says(self) -> None:
        rows = _recent_says()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if not rows:
            self.text.insert("end", "今天还没有开口。\n", "d")
        for row in rows:
            at = str(row.get("at") or "")
            who = str(row.get("who") or row.get("target") or "")
            self.text.insert("end", "%s  %s\n" % (at[11:16] if len(at) > 16 else at, who), "d")
            self.text.insert("end", "%s\n" % str(row.get("text") or ""), "t")
            why = str(row.get("why") or "").strip()
            if why:
                self.text.insert("end", "   ↳ %s\n" % (why[:120]), "w")
            self.text.insert("end", "\n")
        self.text.configure(state="disabled")

    # ---------- 按钮 ----------
    def start(self) -> None:
        _spawn(["cmd", "/c", str(ROOT / "启动鲸鲸.bat")])
        self.master.after(3000, self.refresh_once)

    def restart(self) -> None:
        if not messagebox.askokcancel("重启鲸鲸", "会先等这一拍走完，再全关重开。继续？"):
            return
        _spawn(["cmd", "/c", str(ROOT / "重启鲸鲸.bat")])
        self.master.after(5000, self.refresh_once)

    def stop(self) -> None:
        if not messagebox.askokcancel("停止鲸鲸", "监管器、主程序、引擎会一起退。继续？"):
            return
        (LOGS / "all.stop").write_text("stop_from_window", encoding="utf-8")
        self.master.after(2000, self.refresh_once)

    def open_ledger(self) -> None:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(LEDGER_DIR))  # noqa: S606

    def open_logs(self) -> None:
        LOGS.mkdir(parents=True, exist_ok=True)
        os.startfile(str(LOGS))  # noqa: S606

    def refresh_once(self) -> None:
        try:
            self._refresh_status()
            self._refresh_says()
        except Exception:
            pass

    def quit(self) -> None:
        self.master.destroy()


def main() -> int:
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    if "--smoke" in sys.argv:  # 自检：建一次窗口、刷一次、退出（不开 mainloop）
        r = tk.Tk()
        a = App(r)
        a.refresh_once()
        r.update()
        r.destroy()
        print("smoke ok")
        raise SystemExit(0)
    raise SystemExit(main())
