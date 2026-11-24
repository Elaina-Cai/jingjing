"""CB-069 卡正文写回契约的锁：规范化、两种上限、素材体检、写口唯一。"""

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.identity import cardfile  # noqa: E402


def test_normalise_unifies_newlines_and_drops_trailing_blank_lines():
    assert cardfile.normalise("a\r\nb\r\n\r\n\r\n") == "a\nb"
    assert cardfile.normalise("a\rb\r\n") == "a\nb"
    assert cardfile.normalise("") == ""
    assert cardfile.normalise(None) == ""


def test_only_whitespace_lines_are_kept_as_is():
    # 行内（中间）的「只有空格/Tab 的行」必须原样保留：YAML/markdown 会当结构读
    assert cardfile.normalise("a\n  \t \nb\n\n\n") == "a\n  \t \nb"
    # 行尾两个空格是 markdown 的硬换行，属于内容，不许被 strip 掉
    assert cardfile.normalise("a  \n\n\n") == "a  "


def test_byte_limit_blocks_and_char_line_only_warns():
    ok = cardfile.check("很短的一段")
    assert ok["ok"] and not ok["errors"] and not ok["warnings"]

    big = "中" * 45000            # 45000 字 ≈ 135000 字节 > 131072
    rep = cardfile.check(big)
    assert rep["errors"], rep
    assert any("字节" in e for e in rep["errors"])

    warn = cardfile.check("字" * (cardfile.MAX_CHARS + 1))
    assert not warn["errors"] and warn["warnings"], warn


def test_missing_asset_is_a_warning_not_a_blocker():
    # 钉住一个事实：export_blockers 抓不到「素材不存在」，所以存在性只能自己问
    found = cardfile.assets("no_such_slug_zz")
    assert found["absent"] == ["faces", "voice"]
    assert found["blockers"] == []
    assert found["warnings"]


def test_license_blocked_card_is_saved_with_a_warning(monkeypatch, tmp_path):
    # 「素材不可分发」不等于「卡不能存」：卡里只是一句 slug，许可是出包时的事。
    # 真拦会把鲸鲸那张卡锁死（faces/voice 都是 license_forbids）→ 用户改不了她的卡。
    monkeypatch.setattr(cardfile, "assets", lambda slug="", kinds=("faces", "voice"): {
        "slug": slug, "absent": [], "warnings": [], "blockers": [
            {"kind": "faces", "reason": "license_forbids",
             "source": "某处", "note": ""}]})
    dst = tmp_path / "x.md"
    rep = cardfile.write(str(dst), {"name": "x"}, "正文")
    assert rep["ok"], rep
    assert dst.exists()
    assert any("出包" in w and "faces" in w for w in rep["warnings"]), rep["warnings"]


def test_real_card_with_licensed_assets_is_editable():
    # 用真实数据钉住：jingjing 的 faces/voice 都是 license_forbids，但存卡必须能过
    found = cardfile.assets("jingjing")
    assert {b["kind"] for b in found["blockers"]} >= {"faces"}, found
    rep = cardfile.write("unused.md", {"name": "鲸鲸", "looks": "jingjing"}, "正文", dry_run=True)
    assert rep["ok"], rep
    assert any("出包" in w for w in rep["warnings"]), rep["warnings"]


def test_write_roundtrips_with_lf_only(monkeypatch, tmp_path):
    monkeypatch.setattr(cardfile, "assets", lambda slug="", kinds=("faces", "voice"): {
        "slug": slug, "absent": [], "warnings": [], "blockers": []})
    dst = tmp_path / "y.md"
    rep = cardfile.write(str(dst), {"name": "y", "voice": "", "looks": ""},
                         "第一行\r\n  只空格的行\r\n末行  \r\n\r\n")
    assert rep["ok"], rep
    raw = dst.read_bytes()
    assert b"\r\n" not in raw
    text = raw.decode("utf-8")
    assert text.startswith("name: y\n")
    assert "  只空格的行\n" in text
    assert "末行  \n" in text            # 行尾两空格没被吃掉
    assert text.endswith("末行  \n")     # 只有一个结尾换行


def test_executor_uses_the_contract_not_its_own_strip():
    src = (ROOT / "console/executor.py").read_text(encoding="utf-8-sig")
    assert 'str(patch.get("text") or "").strip()' not in src, "卡正文又被整块 strip 了"
    assert src.count("_cardfile.join(meta, body)") == 2
    assert src.count("_cardfile.check(") >= 1
    assert src.count("_cardfile.assets(") >= 1


def test_limits_are_one_definition():
    from console import executor
    assert cardfile.MAX_CHARS == 20000
    assert cardfile.MAX_BYTES == 131072
    assert executor._MD_CAP == cardfile.MAX_CHARS, "面板显示的上限必须和契约同一个数"


def test_scan_is_not_vacuous():
    # 断言真的在测东西：函数存在、且 normalise 不是恒等
    for name in ("normalise", "check", "assets", "join", "write"):
        assert callable(getattr(cardfile, name))
    assert cardfile.normalise("a\r\n\r\n") != "a\r\n\r\n"
