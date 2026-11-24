"""交付形态渲染（`core/artifacts/render.py`）的验收。

跑法：python -X utf8 -u tests/test_artifact_render.py
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.artifacts import render as R  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 临时目录的唯一入口（见 tests/_tempdirs.py）：不给 dir= 时系统 TEMP 不可写会静默回落到仓库根。
from _tempdirs import temp_base  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    if cond:
        print(f"PASS {name}")
        return
    _FAILED += 1
    print(f"FAIL {name} {detail}")


def main() -> int:
    # ---------- 1. 形状 ----------
    check("五种形态，封闭集合", R.KINDS == ("text", "file", "card", "page", "link"), str(R.KINDS))
    check("check_kind 归一大小写与空格", R.check_kind("  CARD ") == "card")
    try:
        R.check_kind("video")
        check("不认的形态要抛", False)
    except R.ArtifactError as exc:
        check("不认的形态要抛", "unknown_kind" in str(exc), str(exc))

    # ---------- 2. 文件名 ----------
    check("slug 留中文", R.slugify("下周杭州行程") == "下周杭州行程")
    check("slug 去非法字符", R.slugify('a/b:c*?"<>|d') == "abcd", R.slugify('a/b:c*?"<>|d'))
    check("slug 空标题有兜底", R.slugify("   ") == "artifact")
    check("slug 有长度上限", len(R.slugify("很长" * 40)) <= 40, str(len(R.slugify("很长" * 40))))

    # ---------- 3. 伴随语 ----------
    check("伴随语 ≤40 字", len(R.accompany_text("下周杭州行程")) <= R.ACCOMPANY_MAX_CHARS)
    long_title = "行程" * 40
    acc = R.accompany_text(long_title)
    check("长标题截断并带省略号", len(acc) <= R.ACCOMPANY_MAX_CHARS and acc.endswith("…"), acc)

    # ---------- 4. 卡片（纯函数） ----------
    png, truncated = R.render_card_png("下周杭州行程", "周五 18:00 到杭州东\n周六 西湖\n周日 回程")
    check("卡片是 PNG", png[:8] == b"\x89PNG\r\n\x1a\n", str(png[:8]))
    check("短正文不截断", truncated is False)
    check("卡片有内容", len(png) > 5000, str(len(png)))

    big = "行" * (R.MAX_CARD_CHARS + 100)
    _png2, truncated2 = R.render_card_png("超长", big)
    check("超长正文标 truncated", truncated2 is True)

    try:
        R.render_card_png("空", "   ")
        check("空正文要抛", False)
    except R.ArtifactError as exc:
        check("空正文要抛", "empty_body" in str(exc), str(exc))

    # ---------- 5. 落盘 ----------
    with tempfile.TemporaryDirectory(prefix="artifact_", dir=str(temp_base())) as tmp:
        p1 = R.write_card("下周杭州行程", "第一天 西湖", out_dir=tmp)
        p2 = R.write_card("下周杭州行程", "第一天 西湖", out_dir=tmp)
        check("卡片落盘", Path(p1).exists() and Path(p1).suffix == ".png", p1)
        check("同名不覆盖", p1 != p2 and Path(p2).exists(), f"{p1} / {p2}")
        check("落的是 PNG", io.open(p1, "rb").read(8) == b"\x89PNG\r\n\x1a\n")

        page = R.write_page("行程笔记", "第一天 西湖", out_dir=tmp)
        text = io.open(page, encoding="utf-8").read()
        check("页落盘且是 md", Path(page).suffix == ".md")
        check("页里有标题与生成时间", "行程笔记" in text and "鲸鲸生成" in text, text[:60])

        # ---------- 6. 翻译成交付 ----------
        text_item = R.to_deliver("text", body="在的，我看下", target="wxid_me")
        check("text → say", text_item.kind == "say" and text_item.text == "在的，我看下",
              str(text_item))

        link_item = R.to_deliver("link", url="https://example.com/a", target="wxid_me")
        check("link → say 且是链接", link_item.kind == "say" and link_item.text.startswith("https://"))
        try:
            R.to_deliver("link", url="javascript:alert(1)")
            check("非 http(s) 链接要抛", False)
        except R.ArtifactError:
            check("非 http(s) 链接要抛", True)

        card_item = R.to_deliver("card", title="下周杭州行程", body="第一天 西湖",
                                 target="wxid_me", out_dir=tmp, turn_id="t1")
        check("card → file", card_item.kind == "file" and card_item.file.endswith(".png"),
              str(card_item))
        check("card 的伴随语 ≤40", len(card_item.text) <= R.ACCOMPANY_MAX_CHARS, card_item.text)
        check("card 真的落了盘", Path(card_item.file).exists())

        page_item = R.to_deliver("page", title="行程笔记", body="第一天", target="wxid_me",
                                 out_dir=tmp)
        check("page → file", page_item.kind == "file" and page_item.file.endswith(".md"))

        try:
            R.to_deliver("file", title="不存在", path=str(Path(tmp) / "nope.txt"))
            check("找不到的文件要抛（不许伪造路径）", False)
        except R.ArtifactError as exc:
            check("找不到的文件要抛（不许伪造路径）", "file_missing" in str(exc), str(exc))

        real = Path(tmp) / "real.txt"
        real.write_text("hi", encoding="utf-8")
        file_item = R.to_deliver("file", title="附件", path=str(real), target="wxid_me")
        check("file → file 且路径已解析", file_item.kind == "file" and Path(file_item.file).is_absolute())

        # ---------- 7. 那一槽的形状 ----------
        art = R.artifact_of("card", renderer="card_png", ttl_s=86400)
        check("artifact_of 造出 Artifact", art.kind == "card" and art.ttl_s == 86400, str(art))

        # ---------- 8. describe ----------
        check("describe 说清交付了什么", "png" in R.describe(card_item), R.describe(card_item))
        check("describe 纯文字", "6 字" in R.describe(text_item), R.describe(text_item))

    # ---------- 9. 静态纪律 ----------
    src = io.open(ROOT / "core/artifacts/render.py", encoding="utf-8").read()
    check("不 import speech", "core.speech" not in src)
    check("不 import routing", "core.routing" not in src)
    check("不 import agents", "agents" not in src.replace("agentic", ""))
    check("不碰网络", "requests" not in src and "urlopen" not in src)
    check("没有 shell", "subprocess" not in src and "os.system" not in src)

    print(f"RESULT {_FAILED} failed")
    return _FAILED


if __name__ == "__main__":
    raise SystemExit(main())
