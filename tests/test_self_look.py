# -*- coding: utf-8 -*-
"""形象包（faces）的判定契约。

这些断言是开源用户拿走代码后必须仍然成立的：包是数据，代码不认识任何角色。
fixture 里的角色是空编的（圆耳朵 + 条纹衫），与仓库里任何一个真实角色无关。

两个反例值得记住，都是 2026-10-26 真踩出来的：
1. 纯色图 dhash 恒为 0 → 会被认成"自己"，必须靠 is_flat 拒答；
2. 同版式不同配色 dhash 也能撞 → 必须再过平均色闸门。
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw

from core.identity import self_look as sl


def _textured(path: Path, *, bg=(20, 90, 210), ink=(240, 240, 250)) -> Path:
    """造一张有结构的图：斜向渐变 + 高对比形体（dhash 才有信息量）。"""
    w = h = 96
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im)
    for x in range(w):
        k = x / max(1, w - 1)
        d.line([(x, 0), (x, h)], fill=(int(bg[0] * k), int(bg[1] * k), int(bg[2] * k)))
    d.ellipse([w // 4, h // 4, w // 2, h // 2], fill=ink)
    d.rectangle([w // 2, h // 2, w - 8, h - 8], outline=ink, width=3)
    im.save(path)
    return path


PACK = {
    "slug": "demo",
    "thresholds": {"dhash_max_distance": 6, "color_max_distance": 24,
                   "text_self": 0.7, "text_maybe": 0.45, "text_scale": 6.0},
    "veto_terms": ["白发", "银白", "紫发"],
    "slots": {
        "ears": {"weight": 3, "terms": ["圆耳朵", "大圆耳"], "identity": True,
                 "hint": "头顶是不是一对圆耳朵"},
        "outfit": {"weight": 2, "terms": ["条纹衫", "围裙"]},
        "mascot": {"weight": 3, "terms": ["小鲸鱼"], "identity": True},
    },
}


def _build_pack(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    assets = tmp_path / "assets"
    faces = assets / "demo" / "faces"
    gallery = faces / "gallery"
    gallery.mkdir(parents=True)
    main = _textured(gallery / "a.png", bg=(20, 90, 210))
    other = _textured(gallery / "b.png", bg=(210, 90, 20), ink=(30, 30, 40))
    (faces / "index.json").write_text(json.dumps({
        "slug": "demo",
        "images": [{"file": f.name, "md5": sl.md5_of(f), "dhash": sl.dhash(f)}
                   for f in (main, other)],
    }, ensure_ascii=False), encoding="utf-8")
    (faces / "pack.json").write_text(json.dumps(PACK, ensure_ascii=False), encoding="utf-8")
    (faces / "canon.txt").write_text("我有一对圆耳朵。\n", encoding="utf-8")
    monkeypatch.setattr(sl, "ASSETS", assets)
    return faces, main


# ---- 看图判定：md5 / dhash ----

def test_canon_and_block(tmp_path, monkeypatch):
    _build_pack(tmp_path, monkeypatch)
    assert sl.canon_text("demo") == "我有一对圆耳朵。"
    assert sl.self_block("demo").splitlines()[0].startswith("[我的样子]")  # assembler 用 startswith 认块


def test_exact_image_is_self(tmp_path, monkeypatch):
    _faces, main = _build_pack(tmp_path, monkeypatch)
    got = sl.match_self(main, slug="demo")
    assert got["is_self"] == "self" and got["level"] == "md5"


def test_recompressed_image_is_self(tmp_path, monkeypatch):
    _faces, main = _build_pack(tmp_path, monkeypatch)
    small = tmp_path / "small.jpg"
    with Image.open(main) as im:
        im.resize((64, 64)).save(small, quality=60)
    got = sl.match_self(small, slug="demo")
    assert got["is_self"] == "self" and got["level"] == "dhash"


def test_unrelated_image_is_unknown(tmp_path, monkeypatch):
    _faces, _main = _build_pack(tmp_path, monkeypatch)
    stranger = _textured(tmp_path / "stranger.png", bg=(30, 160, 40))
    assert sl.match_self(stranger, slug="demo")["is_self"] == "unknown"


def test_flat_image_refuses_to_judge(tmp_path, monkeypatch):
    """纯色图没有结构：dhash 撞车不等于同一张图。"""
    _faces, _main = _build_pack(tmp_path, monkeypatch)
    blank = tmp_path / "blank.png"
    Image.new("RGB", (96, 96), (255, 255, 255)).save(blank)
    got = sl.match_self(blank, slug="demo")
    assert got["is_self"] == "unknown" and got["why"] == "low_detail"


def test_same_layout_different_palette_is_unknown(tmp_path, monkeypatch):
    """排版一样、配色不同的图，dhash 会撞；平均色闸门必须拦住。"""
    _faces, _main = _build_pack(tmp_path, monkeypatch)
    recolor = tmp_path / "recolor.png"
    _textured(recolor, bg=(30, 160, 40), ink=(250, 250, 250))
    got = sl.match_self(recolor, slug="demo")
    assert got["is_self"] == "unknown"


def test_missing_file_and_missing_pack_do_not_raise(tmp_path, monkeypatch):
    assert sl.match_self(tmp_path / "nope.jpg", slug="demo")["why"] == "no_file"
    monkeypatch.setattr(sl, "ASSETS", tmp_path / "empty")
    assert sl.match_self(__file__, slug="demo")["why"] == "no_pack"


# ---- 文字判定：描述里本来就有外貌词，不必再问一次视觉 ----

def test_judge_text_recognises_her(tmp_path, monkeypatch):
    _build_pack(tmp_path, monkeypatch)
    for desc in ("圆耳朵条纹衫的小女孩举着手。", "小鲸鱼抱着圆耳朵睡觉。"):
        got = sl.judge_text(desc, slug="demo")
        assert got["verdict"] == "self", got


def test_judge_text_needs_identity_evidence(tmp_path, monkeypatch):
    """只有发色/衣服这类"满大街都有"的词，不下结论。"""
    _build_pack(tmp_path, monkeypatch)
    got = sl.judge_text("穿条纹衫的路人走过街口。", slug="demo")
    assert got["verdict"] == "unknown" and "no_identity_evidence" in got["why"]


def test_judge_text_vetoes_other_looks(tmp_path, monkeypatch):
    _build_pack(tmp_path, monkeypatch)
    got = sl.judge_text("白发圆耳朵的小女孩站着。", slug="demo")
    assert got["verdict"] == "unknown" and got["vetoed"] is True, got


def test_judge_text_rejects_unrelated(tmp_path, monkeypatch):
    _build_pack(tmp_path, monkeypatch)
    for desc in ("戴蓝帽的老人眯眼看手机。", "一只白底黑斑猫躺着舔毛。"):
        assert sl.judge_text(desc, slug="demo")["verdict"] == "unknown", desc


# ---- 开关 ----

def test_switch_off(tmp_path, monkeypatch):
    _faces, main = _build_pack(tmp_path, monkeypatch)
    monkeypatch.setenv("YINYUE_SELF_LOOK", "off")
    assert sl.enabled() is False
    assert sl.match_self(main, slug="demo")["why"] == "disabled"
    assert sl.judge_text("圆耳朵小鲸鱼", slug="demo")["why"] == "disabled"
    assert sl.self_block("demo") == ""


# ---- 写描述时的提示（P2.5） ----

def test_describe_hint_lists_slot_features(tmp_path, monkeypatch):
    _build_pack(tmp_path, monkeypatch)
    hint = sl.describe_hint("demo")
    assert "圆耳朵" in hint, hint          # slot 的 hint 字段原样拼进去
    assert "看不出来" not in hint          # 提示只描述看得见什么，不许她猜身份


def test_describe_hint_off(tmp_path, monkeypatch):
    _build_pack(tmp_path, monkeypatch)
    monkeypatch.setenv("YINYUE_SELF_LOOK", "off")
    assert sl.describe_hint("demo") == ""


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    for _p in (str(_Path(__file__).resolve().parent.parent), str(_Path(__file__).resolve().parent)):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    from _mini_pytest import run_all as _run_all

    _sys.exit(0 if _run_all(globals()) else 1)
