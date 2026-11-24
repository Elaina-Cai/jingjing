"""match_active_chat() 不许把 _header_print() 的矩形坐标当标题文字比对。

2026-11-01 事故：_header_print() 返回 ((x,y,w,h), RGB ndarray)，而
match_active_chat() 里写成 _got = "".join(str(x) for x in header)，
于是 got="3774890471"，与 display 必然不等 → 每次发送后都
invalidate_active_chat("title_mismatch") → 刚建立的可信记忆被作废，
下一条又从 6-7 秒的标题 OCR 重来（指纹 0.1 秒的路一次都走不通）。
"""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.execution import wx_chat  # noqa: E402


def test_guard_does_not_join_header_rect_as_text() -> None:
    src = inspect.getsource(wx_chat.match_active_chat)
    assert "for x in header" not in src, "又把矩形坐标当标题文字了"


def test_guard_short_circuits_on_matching_pixels() -> None:
    src = inspect.getsource(wx_chat.match_active_chat)
    fast = src.find("_print_matches(fingerprint")
    ocr = src.find("ocr_items(")
    assert fast != -1 and ocr != -1, (fast, ocr)
    assert fast < ocr, "像素一致时要先短路返回，不给 OCR 付费"


def test_guard_reads_title_text_from_ocr() -> None:
    src = inspect.getsource(wx_chat.match_active_chat)
    assert "_items = wx_vision.ocr_items(fingerprint)" in src


def test_header_print_returns_rect_and_pixels_not_text() -> None:
    src = inspect.getsource(wx_chat._header_print)
    assert "return (x, y, w, h), array" in src
def main() -> bool:
    """2026-11-02 补：这个文件以前只有 def test_、没有运行入口（一直空转）。"""
    names = sorted(n for n in globals() if n.startswith("test_"))
    fails = []
    for n in names:
        fn = globals()[n]
        if not callable(fn):
            continue
        try:
            fn()
            print("  ok   %s" % n)
        except Exception as exc:  # noqa: BLE001 - 汇总用，单个用例炸了要继续跑完
            fails.append(n)
            print("  FAIL %s  %s" % (n, repr(exc)[:200]))
    print("RESULT %d failed / %d" % (len(fails), len(names)))
    return not fails


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
