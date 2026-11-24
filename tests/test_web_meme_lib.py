# -*- coding: utf-8 -*-
"""web 梗三层库检查：索引合并/标题优先/状态保留 + provider 只召回 approved + hub 接线。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "scripts"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import build_meme_index as bmi  # noqa: E402
from core.memory.providers import WebMemeMemoryProvider  # noqa: E402
from _tempdirs import make_db_dir


def _write_extracted(base: Path, bvid: str, title: str, memes) -> Path:
    d = base / "extracted"
    d.mkdir(parents=True, exist_ok=True)
    (d / (bvid + ".json")).write_text(
        json.dumps({"bvid": bvid, "title": title, "memes": memes}, ensure_ascii=False),
        encoding="utf-8",
    )
    return d


def test_index_merge_title_priority_and_status():
    base = Path(make_db_dir("meme_lib_"))
    ext = _write_extracted(base, "BV1", "专是专本是本是什么梗【梗指南】", [
        {"key": "专是专本", "aliases": ["专是专本是本"], "meaning": "短", "example": "例1"},
        {"key": "望砖", "meaning": "望砖释义", "example": "例2"},
    ])
    _write_extracted(base, "BV2", "再谈望砖与专升本", [
        {"key": "望砖", "meaning": "望砖释义（更长的版本）", "example": "例3"},
    ])
    rows = [("BV1", "专是专本是本是什么梗【梗指南】", "1791950700"), ("BV2", "再谈望砖与专升本", "1792000000")]
    (base / "video_list.tsv").write_text(
        chr(10).join(chr(9).join(r) for r in rows) + chr(10), encoding="utf-8")
    bmi.EXTRACTED = ext
    bmi.INDEX = base / "memes_index.json"
    bmi.VIDEO_LIST = base / "video_list.tsv"
    assert bmi.main() == 0
    data = json.loads(bmi.INDEX.read_text(encoding="utf-8"))
    assert data["videos"] == 2 and data["count"] == 2, data
    by = {e["key"]: e for e in data["entries"]}
    assert "专是专本是本" in by, by.keys()
    assert "专是专本" in by["专是专本是本"]["aliases"]
    wang = by["望砖"]
    assert wang["mentions"] == 2 and len(wang["videos"]) == 2
    assert "更长的版本" in wang["meaning"]
    assert all(e["status"] == "pending" for e in data["entries"])
    data["entries"][0]["status"] = "approved"
    bmi.INDEX.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert bmi.main() == 0
    data2 = json.loads(bmi.INDEX.read_text(encoding="utf-8"))
    assert any(e["status"] == "approved" for e in data2["entries"])


def test_provider_only_approved():
    base = Path(make_db_dir("meme_prov_"))
    index = base / "idx.json"
    index.write_text(json.dumps({"entries": [
        {"key": "望砖", "aliases": ["忘专"], "meaning": "上岸后抛弃专科出身", "heat": 2,
         "status": "approved", "videos": [{"bvid": "BV1"}]},
        {"key": "专是专本是本", "aliases": [], "meaning": "待审", "heat": 9,
         "status": "pending", "videos": []},
    ]}, ensure_ascii=False), encoding="utf-8")
    prov = WebMemeMemoryProvider(index_path=index)
    got = prov.recall(None, query="望砖", limit=5)
    assert len(got) == 1 and got[0].kind == "meme" and "上岸后抛弃专科出身" in got[0].content
    assert got[0].metadata.get("source") == "web" and got[0].metadata.get("bvid") == "BV1"
    assert prov.recall(None, query="忘专", limit=5)
    # 2026-10-16: single-char queries no longer substring-match (探针: "梗"/"6" 噪声).
    assert prov.recall(None, query="不存在", limit=5) == []
    assert prov.recall(None, query="专是专本是本", limit=5) == []


def test_hub_registers_web_provider():
    src = (ROOT / "core/memory/hub.py").read_text(encoding="utf-8")
    assert "WebMemeMemoryProvider" in src
    assert "hub.register(WebMemeMemoryProvider())" in src
    app = (ROOT / "scripts/approve_memes.py").read_text(encoding="utf-8")
    assert "approved" in app and "denied" in app
    # 该设计文档在 4e2b8ce 被主动删掉；原来那句断言本身还是恒真式
    # （`A and B or C`，且 C 几乎必然成立），删掉不损失任何保护。




def test_provider_short_key_noise_guard():
    """Short keys (梗/6/yee) must not match by substring; exact query still hits."""
    base = Path(make_db_dir("meme_prov_"))
    index = base / "idx.json"
    index.write_text(json.dumps({"entries": [
        {"key": "梗", "aliases": [], "meaning": "字典条目本身", "heat": 1,
         "status": "approved", "videos": []},
        {"key": "望砖", "aliases": ["忘专"], "meaning": "上岸后抛弃专科出身", "heat": 2,
         "status": "approved", "videos": []},
    ]}, ensure_ascii=False), encoding="utf-8")
    prov = WebMemeMemoryProvider(index_path=index)
    assert prov.recall(None, query="这里是什么梗/暗号", limit=5) == []
    assert prov.recall(None, query="梗", limit=5), "exact key must still match"
    assert prov.recall(None, query="忘", limit=5) == []
TESTS = [test_index_merge_title_priority_and_status, test_provider_only_approved, test_provider_short_key_noise_guard, test_hub_registers_web_provider]


def main() -> bool:
    ok = True
    for t in TESTS:
        try:
            t()
        except AssertionError as exc:
            print("FAIL " + t.__name__ + ": " + str(exc))
            ok = False
        else:
            print("PASS " + t.__name__)
    if ok:
        print("web-meme-lib checks: " + str(len(TESTS)) + "/" + str(len(TESTS)) + " passed")
    return ok


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
