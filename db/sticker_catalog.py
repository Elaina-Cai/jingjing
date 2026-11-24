# -*- coding: utf-8 -*-
"""表情素材的"落库"层（2026-11-01）。

为什么要它：面板上的"同图合并"是**实时算**的（像素哈希 + dhash，60 秒缓存），
换台机器、清个缓存就得从头再算一遍；而且"登记一张 = 登记整簇"需要知道
簇里有谁。这里把结果落到 ``data/stickers/catalog.db``：

- ``asset``   —— 一个本地文件（md5 = 文件名，也就是平台的去重键）
- ``cluster`` —— 一个视觉实体（``px`` = 归一化 32×32 像素的 md5），``members`` 是它的全部文件

只读现有缓存目录，不搬家、不改名、不删文件。算不动的图（解不开）记 ``fmt='?'`` 留着。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time

_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "stickers", "catalog.db")

_SCHEMA = """
create table if not exists asset(
  md5 text primary key, path text, dir text, ext text, size integer,
  fmt text, frames integer, w integer, h integer, px text, dhash text,
  seen_ts real, updated_ts real);
create table if not exists cluster(
  px text primary key, rep text, n integer, members text, updated_ts real);
create index if not exists ix_asset_px on asset(px);
create table if not exists meta(k text primary key, v text);
"""


def _conn():
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    c = sqlite3.connect(_DB, timeout=5.0)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def signature(path: str) -> dict:
    """算一张图的签名：归一化像素哈希（同图必同）+ dhash（近似图）+ 尺寸/帧数。"""
    from PIL import Image

    out = {"fmt": "?", "frames": 1, "w": 0, "h": 0, "px": "", "dhash": ""}
    try:
        with Image.open(path) as im:
            out["fmt"] = (im.format or "?").upper()
            out["w"], out["h"] = im.size
            out["frames"] = int(getattr(im, "n_frames", 1) or 1)
            g = im.convert("RGB").resize((32, 32), Image.LANCZOS)
            out["px"] = hashlib.md5(g.tobytes()).hexdigest()[:16]
            small = g.convert("L").resize((9, 8), Image.LANCZOS)
            bits = 0
            px = list(small.getdata())
            for row in range(8):
                for col in range(8):
                    left = px[row * 9 + col]
                    right = px[row * 9 + col + 1]
                    bits = (bits << 1) | (1 if left > right else 0)
            out["dhash"] = "%016x" % bits
    except Exception as exc:
        # 解不开的图留着（fmt='?'），但要把原因记下来：静默 pass 会让"这张图没有簇"
        # 看起来像"它本来就独一无二"。
        out["error"] = repr(exc)[:120]
    return out


def _scan_dirs() -> list:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dirs = [os.path.join(root, "data", "emoji_cache")]
    return [d for d in dirs if os.path.isdir(d)]


def sync(*, force: bool = False) -> dict:
    """扫一遍本地缓存，把结果写进库。返回 {files, clusters, dups, ms}。"""
    t0 = time.time()
    now = time.time()
    rows = []
    for d in _scan_dirs():
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            md5 = os.path.splitext(name)[0]
            rows.append((md5, p, d, os.path.splitext(name)[1].lower(), os.path.getsize(p)))
    with _conn() as c:
        known = {r["md5"]: r["updated_ts"] for r in c.execute("select md5, updated_ts from asset")}
        todo = [r for r in rows if force or r[0] not in known]
        for md5, p, d, ext, size in todo:
            sig = signature(p)
            c.execute("insert or replace into asset(md5,path,dir,ext,size,fmt,frames,w,h,px,dhash,seen_ts,updated_ts)"
                      " values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (md5, p, d, ext, size, sig["fmt"], sig["frames"], sig["w"], sig["h"],
                       sig["px"], sig["dhash"], known.get(md5, now), now))
        # 重建簇：同 px 的归一个簇，代表取"像素面积最大、再 md5 字典序"
        c.execute("delete from cluster")
        buckets = {}
        for r in c.execute("select md5, px, w, h from asset where px != ''"):
            buckets.setdefault(r["px"], []).append((r["w"] * r["h"], r["md5"]))
        for px, items in buckets.items():
            items.sort(key=lambda x: (-x[0], x[1]))
            c.execute("insert or replace into cluster(px,rep,n,members,updated_ts) values(?,?,?,?,?)",
                      (px, items[0][1], len(items), json.dumps([m for _, m in items]), now))
        c.execute("insert or replace into meta(k,v) values('synced_ts',?)", (str(now),))
        c.commit()
        n_asset = c.execute("select count(*) c from asset").fetchone()["c"]
        n_cl = c.execute("select count(*) c from cluster").fetchone()["c"]
    return {"files": int(n_asset), "clusters": int(n_cl), "dups": int(n_asset - n_cl),
            "scanned": len(todo), "ms": int((time.time() - t0) * 1000)}


def clusters() -> list:
    with _conn() as c:
        return [{"px": r["px"], "rep": r["rep"], "n": int(r["n"]),
                 "members": json.loads(r["members"] or "[]")} for r in
                c.execute("select * from cluster order by n desc, rep")]


def members_of(md5: str) -> list:
    """这个文件所在簇的全部成员（含自己）；查不到就只返回它自己。"""
    m = str(md5 or "").strip().lower()
    if not m:
        return []
    with _conn() as c:
        r = c.execute("select px from asset where md5=?", (m,)).fetchone()
        if not r or not r["px"]:
            return [m]
        cl = c.execute("select members from cluster where px=?", (r["px"],)).fetchone()
        if not cl:
            return [m]
        out = json.loads(cl["members"] or "[]")
        return out or [m]


def stats() -> dict:
    with _conn() as c:
        a = c.execute("select count(*) c from asset").fetchone()["c"]
        cl = c.execute("select count(*) c from cluster").fetchone()["c"]
        m = c.execute("select v from meta where k='synced_ts'").fetchone()
    return {"files": int(a), "clusters": int(cl), "dups": int(a - cl),
            "synced_at": float(m["v"]) if m else 0.0}
