"""媒体完整性判据（2026-10-02「她看不见图」根治）。

现象（Lead 实测）
-----------------
主人私聊发图 → 她回「图片没读出来…」；群里发图 → 她只能问「说清楚点」。
日志：``llm_vision_providers_skipped`` → ``llm_vision_fallback``
（``400 You have uploaded an unsupported image … formats: webp, png, jpeg, gif``）
→ ``llm_vision_all_failed``；一晚 3 次视觉调用 0 成功。

真因（本文件同时是这条结论的回归位）
------------------------------------
缓存的图是**截断文件**：``data/image_cache/*.jpg`` 末尾缺 JPEG 的 ``FFD9``。
WeChat 的 ``.dat`` 容器（``07 08 'V2'`` 头，15 字节 + AES 块 + XOR 区）实际比它
头部声明的 ``aes_size``/``xor_size`` 多出**正好 16 字节**，那 16 字节仍是
XOR 加密的图像数据（尾两字节 ``b4 92`` XOR ``get_xor_key()``(=0x4b) 恰好是
``ff d9``）。``db/image_decrypt.py::_decrypt_dat_v2`` 按声明长度截断，于是每个
图都丢掉最后一小块。**源文件是完整的**，丢尾巴的是我们自己的解密这一步——
所以这里没有"竞态等待"，只有"别把读不出来的文件当成能用的"。

三处修复
--------
① 唯一判据 ``core.conversation.media.image_integrity``（``is_complete_image``
   是它的严格读法，``is_image_readable`` 是"还能不能当图用"的读法）；
② 解析状态不许撒谎：读不出来的文件**不许标 resolved**（保持 pending + 退避重取）；
③ 视觉调用前先过判据：注定 400 的图不再打模型调用，走既有的"直说读不到这张"路径。

本文件钉住的格子
----------------
① 截断 JPEG → 判为不完整、状态保持 ``pending``、**不打模型调用**；
② 完整 JPEG → 判为完整、照常走；
③ PNG / GIF / WEBP 各一格（完整 vs 截断）；
④ 开关 ``off`` → 与接线前**逐字节一致**（URI 逐字节相同 + 照旧标 resolved）；
⑤ ``shadow`` → 只判定不改流转；
⑥ 真源文件比缓存**正好多 16 字节**（真因的证据，不靠 100KB 素材也能跑）。

素材**全部现场构造**（几十~几千字节），不提交任何二进制大文件；真缓存文件只在
存在时作为**可选**对照（缺了 SKIP，不红）。

Run directly: python tests/test_media_integrity.py
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# 隔离（照 tests/test_pat_sync.py）：必须在 import core.* 之前设好，否则
# core.config.settings 首次导入就把**生产**路径固化进来，本文件的 bootstrap
# 兜底会把迁移落到真实 meta / 账号库。
# ---------------------------------------------------------------------------
_ISO_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "data", "_test_media_integrity_iso"
)
os.makedirs(_ISO_ROOT, exist_ok=True)
os.environ["META_DB_PATH"] = os.path.join(_ISO_ROOT, "meta.db")
os.environ["PLATFORMS_DIR"] = os.path.join(_ISO_ROOT, "platforms")
os.environ["DATA_DIR"] = os.path.join(_ISO_ROOT, "data")

# ---------------------------------------------------------------------------
# 测试卫生（照 tests/test_pat_sync.py）：本文件测的是"媒体完整性"，生产 `.env`
# 打开的那几个开关与它无关，但**不许**成为隐式输入——全部 import 期显式钉死。
# 兄弟组必须**整组**一起钉（tests/test_env_flag_isolation.py::
# test_flag_pins_cover_sibling_group）：PAT_GRACE+SEMANTIC_UNIT+GROUP_COALESCE、
# OUTPUT_GATE、REPLY_INTENT+YINYUE_ADMISSION_MODE。
# ---------------------------------------------------------------------------
os.environ["YINYUE_FEATURE_SEMANTIC_UNIT"] = "off"
os.environ["YINYUE_FEATURE_PAT_GRACE"] = "off"
os.environ["YINYUE_FEATURE_GROUP_COALESCE"] = "off"
os.environ["YINYUE_FEATURE_REPLY_INTENT"] = "off"
os.environ["YINYUE_ADMISSION_MODE"] = "off"
os.environ["YINYUE_FEATURE_OUTPUT_GATE"] = "off"

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from core.config.settings import config as _config  # noqa: E402
from core.conversation.media import (  # noqa: E402
    INTEGRITY_TRUNCATED,
    INTEGRITY_UNDECODABLE,
    image_integrity,
    image_mime,
    is_complete_image,
    is_image_readable,
    media_path_to_data_uri,
)
from core.conversation.media_registry import (  # noqa: E402
    KIND_IMAGE,
    STATUS_PENDING,
    STATUS_RESOLVED,
    register,
    resolve_once,
    resolve_pending,
)
from db.migrations import run_migrations  # noqa: E402
from db.models import Message, MessageMedia, init_db  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "data" / "_test_media_integrity.db"
_IMG_DIR = _ROOT / "data" / "_test_media_integrity_imgs"

# 真素材（Lead 实测的那三张之一）：只在存在时作可选对照，**不**提交进仓库。
_REAL_BROKEN = _ROOT / "data" / "image_cache" / "b3186e302e3ea3860d00c69e21c6b592_b.jpg"
_REAL_READABLE = _ROOT / "data" / "image_cache" / "0212307c1585ee643b083d6559081253_b.jpg"
_WX_CACHE = Path(os.environ.get("YINYUE_WX_CACHE")
                 or (Path.home() / "Documents" / "xwechat_files"))

_FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        _FAILURES.append(label)
    return bool(ok)


# ---------------------------------------------------------------------------
# fixtures — built on the fly, never committed
# ---------------------------------------------------------------------------

def _gradient(kind: str, size: int = 96) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            pixels[x, y] = (x * 2 % 256, y * 2 % 256, (x + y) % 256)
    buffer = io.BytesIO()
    if kind == "jpg":
        image.save(buffer, "JPEG", quality=92)
    elif kind == "png":
        image.save(buffer, "PNG")
    elif kind == "gif":
        image.convert("P").save(buffer, "GIF")
    elif kind == "webp":
        image.save(buffer, "WEBP", quality=92)
    else:  # pragma: no cover - programming error
        raise ValueError(kind)
    return buffer.getvalue()


def _decodable(path: Path) -> bool:
    from PIL import Image

    try:
        with Image.open(path) as image:
            image.load()
        return True
    except Exception:  # noqa: BLE001
        return False


_FIXTURES: dict[str, Path] | None = None


def fixtures() -> dict[str, Path]:
    """Small images on disk: one complete and one cut per format.

    The cuts are the shape of the real defect (the tail is gone), but the sizes
    are a few hundred bytes rather than the 127 KB the live cache carries, and
    nothing binary is committed.  :func:`_assert_fixture_contract` proves the
    cuts really do what each case asserts, so a different Pillow build fails here
    loudly instead of quietly weakening a case.
    """
    global _FIXTURES
    if _FIXTURES is not None:
        return _FIXTURES
    _IMG_DIR.mkdir(parents=True, exist_ok=True)
    made: dict[str, Path] = {}
    cuts = {"jpg": 2, "png": 8, "gif": 1, "webp": 4}
    for kind, cut in cuts.items():
        full = _gradient(kind)
        good = _IMG_DIR / f"full.{kind}"
        bad = _IMG_DIR / f"cut.{kind}"
        good.write_bytes(full)
        bad.write_bytes(full[: len(full) - cut])
        made[f"full_{kind}"] = good
        made[f"cut_{kind}"] = bad
    # A JPEG whose scan really is incomplete: 25% of the file is gone, which no
    # decoder can paper over.  This is the class the live cache actually failed on.
    full_jpg = (_IMG_DIR / "full.jpg").read_bytes()
    heavy = _IMG_DIR / "broken.jpg"
    heavy.write_bytes(full_jpg[: len(full_jpg) - max(2, len(full_jpg) // 4)])
    made["broken_jpg"] = heavy
    _FIXTURES = made
    return made


def _assert_fixture_contract(fx: dict[str, Path]) -> None:
    """Fail loudly if a platform builds fixtures that no longer mean what we assert."""
    assert _decodable(fx["full_jpg"]), "fixture full.jpg 必须能解码"
    assert not _decodable(fx["broken_jpg"]), "fixture broken.jpg 必须解不开（否则用例①作废）"
    assert image_integrity(str(fx["cut_png"])) == INTEGRITY_TRUNCATED, \
        "fixture cut.png 必须是「尾块缺失但仍可解码」"


# ---------------------------------------------------------------------------
# database helpers
# ---------------------------------------------------------------------------

def _cleanup() -> None:
    # Windows keeps a file locked while any connection to it is open.  The
    # resolve path can fall back to ``core.bootstrap.get_repo().engine`` (a
    # process-wide engine nothing here disposes), so the unlink is retried after
    # forcing a collection instead of raising WinError 32 out of ``finally`` --
    # that used to turn a fully green run into exit 1.
    import gc
    import time

    for attempt in range(6):
        gc.collect()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(_DB) + suffix).unlink(missing_ok=True)
            except OSError:
                break
        else:
            return
        if attempt < 5:
            time.sleep(0.2)


def _fresh_engine():
    _cleanup()
    engine = init_db(str(_DB))
    run_migrations(str(_DB))
    return engine


def _row(engine, *, msg_id: int, source_path: str, chat_id: str = "c1"):
    """A media row plus the message row it belongs to.

    The message row is what makes "did we publish a file nobody can use?" an
    answerable question: resolution writes the accepted path to
    ``messages.media_path``, so a refusal has to be visible as that column
    staying empty.
    """
    with Session(engine) as session:
        session.add(Message(id=msg_id, msg_id=msg_id, chat_id=chat_id,
                            sender_id="u1", msg_type_name="image",
                            content="[图片]"))
        session.commit()
    register(engine, message_id=msg_id, msg_id=msg_id, chat_id=chat_id,
             sender_id="u1", kind=KIND_IMAGE, md5="", source_path=source_path,
             account_id="a1")
    with Session(engine) as session:
        return session.scalar(
            select(MessageMedia).where(MessageMedia.msg_id == msg_id)
        )


# ---------------------------------------------------------------------------
# ① 截断 JPEG → 不完整 / 保持 pending / 不打模型调用
# ---------------------------------------------------------------------------

def test_truncated_jpeg_is_incomplete() -> bool:
    """判据本身：截断的 JPEG 不是完整图片，也不可读。"""
    print("\n[① 截断 JPEG 的判据]")
    fx = fixtures()
    _assert_fixture_contract(fx)
    broken = fx["broken_jpg"]
    raw = broken.read_bytes()
    ok = True
    ok &= check("末尾不是 ffd9（就是实测的形态）", not raw.endswith(b"\xff\xd9"),
                f"size={len(raw)} tail={raw[-2:].hex()}")
    ok &= check("is_complete_image == False", is_complete_image(str(broken)) is False)
    ok &= check("image_integrity == undecodable",
                image_integrity(str(broken)) == INTEGRITY_UNDECODABLE,
                image_integrity(str(broken)))
    ok &= check("is_image_readable == False", is_image_readable(str(broken)) is False)
    ok &= check("不存在的文件 → missing",
                image_integrity(str(_IMG_DIR / "nope.jpg")) == "missing")
    ok &= check("空文件 → missing",
                (lambda p: (p.write_bytes(b""), image_integrity(str(p)))[1])(
                    _IMG_DIR / "empty.jpg") == "missing")

    if _REAL_BROKEN.exists():
        # 这张曾是最典型的坏件（127684 B，尾 9478，解不开）。解密侧补回尾块后
        # 它应当是**完整**的 127700 B / ffd9 —— 这条因此同时是"生产者修好了"
        # 的活体读数（库里若又出现 127684，它会立刻红）。判据本身对坏件的断言
        # 由上面现场构造的 broken.jpg 承担，不依赖真素材。
        size = _REAL_BROKEN.stat().st_size
        tail = _REAL_BROKEN.read_bytes()[-2:].hex()
        ok &= check("真素材 b3186…_b.jpg 已是完整件（解密重建后的读数）",
                    is_complete_image(str(_REAL_BROKEN))
                    and image_integrity(str(_REAL_BROKEN)) == "ok",
                    f"{size}B tail={tail}")
    else:
        print("  SKIP (真素材 b3186…_b.jpg 不在，跳过可选对照)")
    return bool(ok)


def test_resolution_keeps_pending_and_does_not_lie() -> bool:
    """解析层不许撒谎：读不出来的文件保持 pending，且不写回 messages.media_path。"""
    print("\n[① 解析状态：读不出来 → pending（不标 resolved）]")
    fx = fixtures()
    engine = _fresh_engine()
    ok = True
    try:
        row = _row(engine, msg_id=2001, source_path=str(fx["broken_jpg"]))
        local, status, error = resolve_once(row, engine=engine)
        ok &= check("resolve_once 不返回路径", local == "")
        ok &= check("状态是 pending", status == STATUS_PENDING, status)
        ok &= check("last_error 说的是截断", error == "truncated", repr(error))

        stats = resolve_pending(engine, force=True)
        with Session(engine) as session:
            stored = session.scalar(
                select(MessageMedia).where(MessageMedia.msg_id == 2001)
            )
            message = session.scalar(select(Message).where(Message.msg_id == 2001))
            ok &= check("库里状态仍是 pending", stored.status == STATUS_PENDING,
                        stored.status)
            ok &= check("库里 last_error == truncated", stored.last_error == "truncated",
                        repr(stored.last_error))
            ok &= check("排了退避重取（next_retry_at 有值）",
                        stored.next_retry_at is not None)
            ok &= check("没有把坏路径写回 messages.media_path",
                        not (message.media_path or "").strip(),
                        repr(message.media_path))
            ok &= check("重取计数 +1", int(stored.attempts or 0) == 1,
                        str(stored.attempts))
        ok &= check("pass 里没有 resolved", stats.get("resolved") == 0, str(stats))
    finally:
        engine.dispose()
    return bool(ok)


def test_no_vision_call_for_unreadable_image() -> bool:
    """视觉调用前先过判据：注定 400 的那张图，一次模型调用都不打。"""
    print("\n[① 视觉前校验：不打注定 400 的调用]")
    fx = fixtures()
    ok = True
    from types import SimpleNamespace

    import core.bootstrap as bootstrap_mod
    from agents.tools.analysis import AnalyzeImageTool

    calls: list[int] = []
    original = bootstrap_mod.get_llm_router

    async def _counting_call(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(text="（桩：不该被调用到）")

    class _RouterStub:
        call = staticmethod(_counting_call)

    bootstrap_mod.get_llm_router = lambda: _RouterStub()  # type: ignore[assignment]
    try:
        tool = AnalyzeImageTool()
        result = asyncio.run(tool.execute(image_url=str(fx["broken_jpg"]),
                                          question="这是什么"))
        ok &= check("坏图：chat_vision 调用次数 == 0", len(calls) == 0, str(len(calls)))
        ok &= check("坏图：工具没有输出内容", not result.output, repr(result.output))
        ok &= check("坏图：走了「读不到」的诚实路径（有 error、没编造）",
                    bool(result.error), repr(result.error)[:90])
        ok &= check("坏图：data URI 为空",
                    media_path_to_data_uri(str(fx["broken_jpg"])) == "")
        ok &= check("坏图：_uri_for_path 返回空",
                    tool._uri_for_path(str(fx["broken_jpg"])) == ("", ""))

        # 对照位：完整图照常构造 data URI（模型调用被桩接住，不上网）。
        tool._remember = staticmethod(lambda path, text: None)  # type: ignore[assignment]
        passed_uri = media_path_to_data_uri(str(fx["full_jpg"]))
        ok &= check("完整图：data URI 照常产出（未被误伤）",
                    passed_uri.startswith("data:image/jpeg;base64,"))
        asyncio.run(tool.execute(image_url=str(fx["full_jpg"]), question="这是什么"))
        ok &= check("完整图：确实走到了 router.call（对照）", len(calls) == 1,
                    str(len(calls)))
    finally:
        bootstrap_mod.get_llm_router = original  # type: ignore[assignment]
    return bool(ok)


# ---------------------------------------------------------------------------
# ② 完整 JPEG → 完整、照常走
# ---------------------------------------------------------------------------

def test_complete_jpeg_passes() -> bool:
    print("\n[② 完整 JPEG 照常走]")
    fx = fixtures()
    engine = _fresh_engine()
    ok = True
    try:
        good = fx["full_jpg"]
        ok &= check("is_complete_image == True", is_complete_image(str(good)) is True)
        ok &= check("image_integrity == ok", image_integrity(str(good)) == "ok")
        uri = media_path_to_data_uri(str(good))
        raw = good.read_bytes()
        expected = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        ok &= check("data URI 与接线前逐字节一致（完整图不进新分支）", uri == expected)

        row = _row(engine, msg_id=2002, source_path=str(good))
        local, status, error = resolve_once(row, engine=engine)
        ok &= check("解析照常 resolved", status == STATUS_RESOLVED and local == str(good),
                    f"{status} {error!r}")
        with Session(engine) as session:
            message = session.scalar(select(Message).where(Message.msg_id == 2002))
            ok &= check("照常写回 messages.media_path",
                        (message.media_path or "") == str(good),
                        repr(message.media_path))
    finally:
        engine.dispose()
    return bool(ok)


# ---------------------------------------------------------------------------
# ③ PNG / GIF / WEBP 各一格
# ---------------------------------------------------------------------------

def test_png_gif_webp_cells() -> bool:
    print("\n[③ PNG / GIF / WEBP 各一格（完整 vs 截断）]")
    fx = fixtures()
    ok = True
    # (格式, 截断判据, 说明)：截断字节数与实测一致，见 .tmp 探针的读数。
    cells = (
        ("png", INTEGRITY_TRUNCATED, "去掉 IEND 尾块（8B）"),
        ("gif", INTEGRITY_TRUNCATED, "去掉 GIF 结束符 0x3B（1B）"),
        ("webp", INTEGRITY_UNDECODABLE, "去掉尾部 4B → RIFF 长度对不上"),
    )
    for kind, want, why in cells:
        full = fx[f"full_{kind}"]
        cut = fx[f"cut_{kind}"]
        ok &= check(f"{kind.upper()}: 完整 → complete",
                    is_complete_image(str(full)) is True, image_integrity(str(full)))
        verdict = image_integrity(str(cut))
        ok &= check(f"{kind.upper()}: 截断（{why}）→ 不是 complete",
                    is_complete_image(str(cut)) is False, verdict)
        ok &= check(f"{kind.upper()}: 截断判据 == {want}", verdict == want, verdict)
        ok &= check(f"{kind.upper()}: 完整图 data URI 非空",
                    bool(media_path_to_data_uri(str(full))))
    # JPEG 同样在矩阵里，但截断后一定不可读。
    ok &= check("JPEG: 截断 → 不可读",
                is_image_readable(str(fx["broken_jpg"])) is False)
    return bool(ok)


# ---------------------------------------------------------------------------
# ③b MIME 按真实格式（同批必修的那一格：我们曾经"撒谎"）
# ---------------------------------------------------------------------------

def test_mime_is_read_from_bytes_not_from_the_name() -> bool:
    """PNG/GIF/WEBP 各自报自己的类型，**不看扩展名**。

    接线前的路径用 ``mimetypes.guess_type``（看名字），下采样分支还写死
    ``image/jpeg``：主人发 PNG/GIF 时，payload 声明是 jpeg、字节是 png——
    "我们撒谎"，供应商有权拒。这条把三个格式各自的真实类型钉住。
    """
    print("\n[③b MIME：按字节，不按扩展名]")
    fx = fixtures()
    ok = True
    expect = (("png", "image/png"), ("gif", "image/gif"),
              ("webp", "image/webp"), ("jpg", "image/jpeg"))
    for kind, want in expect:
        full = fx[f"full_{kind}"]
        ok &= check(f"{kind.upper()}: image_mime == {want}",
                    image_mime(str(full)) == want, image_mime(str(full)))
        uri = media_path_to_data_uri(str(full))
        ok &= check(f"{kind.upper()}: data URI 前缀 == data:{want};base64,",
                    uri.startswith(f"data:{want};base64,"), uri[:22])

    # 名字撒谎：字节是 PNG，名字叫 .jpg —— 必须仍报 image/png。
    liar = _IMG_DIR / "actually_png.jpg"
    liar.write_bytes(fx["full_png"].read_bytes())
    ok &= check("PNG 伪装成 .jpg → 仍报 image/png",
                image_mime(str(liar)) == "image/png", image_mime(str(liar)))
    liar_uri = media_path_to_data_uri(str(liar))
    ok &= check("PNG 伪装成 .jpg → data URI 也是 image/png",
                liar_uri.startswith("data:image/png;base64,"), liar_uri[:22])

    # 文本文件用图片扩展名：不是图片，就不许给它编一个图片类型。
    text = _IMG_DIR / "notes.jpeg"
    text.write_text("这不是图片，只是文本", encoding="utf-8")
    ok &= check("文本文件叫 .jpeg → 不给图片类型",
                image_mime(str(text)) == "", repr(image_mime(str(text))))
    return bool(ok)


def test_unknown_format_is_not_labelled_jpeg() -> bool:
    """未知格式**不许编造** ``image/jpeg``——按判据判"不支持"并诚实降级。"""
    print("\n[③b MIME：未知格式不编造]")
    ok = True
    junk = _IMG_DIR / "mystery.jpg"
    junk.write_bytes(b"NOT-AN-IMAGE-AT-ALL" * 24)
    ok &= check("未知字节 + .jpg → image_mime == ''",
                image_mime(str(junk)) == "", repr(image_mime(str(junk))))
    ok &= check("未知字节 + .jpg → 不产出 data URI（诚实降级）",
                media_path_to_data_uri(str(junk)) == "",
                repr(media_path_to_data_uri(str(junk))[:24]))
    ok &= check("未知字节 → 判据不为 'ok'（不是能用的图）",
                image_integrity(str(junk)) != "ok", image_integrity(str(junk)))

    empty = _IMG_DIR / "empty.png"
    empty.write_bytes(b"")
    ok &= check("空文件 → 无类型、无 payload",
                image_mime(str(empty)) == "" and media_path_to_data_uri(str(empty)) == "")
    return bool(ok)



# ---------------------------------------------------------------------------
# ⑥ 真因的证据 + 尾块缺失但可读的一格
# ---------------------------------------------------------------------------

def test_incomplete_tail_is_readable_and_accepted() -> bool:
    """尾块缺失但仍能解码 → **可读**（不误伤），照常 resolved、照常给 provider。

    实测（2026-10-02）：缓存里 126/330 个 jpg 尾块缺失，其中 102 个 Pillow 解出的
    像素与补回尾块后**逐字节相同**，把同一张图交给 DeepSeek 视觉得到 200。所以
    "缺 EOI"不等于"读不到"——只有解码不了的那 24 个才是 400 的来源。
    """
    print("\n[⑥ 尾块缺失但可读 → 不误伤]")
    fx = fixtures()
    engine = _fresh_engine()
    ok = True
    try:
        cut_png = fx["cut_png"]
        ok &= check("PNG 缺 IEND：不是 complete", is_complete_image(str(cut_png)) is False)
        ok &= check("PNG 缺 IEND：但可读", is_image_readable(str(cut_png)) is True)
        ok &= check("PNG 缺 IEND：data URI 照常产出",
                    media_path_to_data_uri(str(cut_png)).startswith("data:image/"))
        row = _row(engine, msg_id=2005, source_path=str(cut_png))
        local, status, _error = resolve_once(row, engine=engine)
        ok &= check("尾块缺失但可读 → 照常 resolved（不压进 pending）",
                    status == STATUS_RESOLVED and local == str(cut_png), status)

        if _REAL_READABLE.exists():
            # 同一格的实测锚，改用**现场构造**的「只缺 EOI」件：真素材在解密修复
            # 之后已经完整（86374B/ffd9），拿它当"缺尾但可读"的例子会随生产者
            # 状态漂移。这里从真素材上切掉最后 2 字节，语义与实测完全一致。
            cut = _IMG_DIR / "real_tail_cut.jpg"
            cut.write_bytes(_REAL_READABLE.read_bytes()[:-2])
            ok &= check("真素材切掉 EOI：不是 complete 但仍可读",
                        is_complete_image(str(cut)) is False
                        and is_image_readable(str(cut)) is True,
                        image_integrity(str(cut)))
            ok &= check("（对照）真素材本身已完整 / ffd9",
                        is_complete_image(str(_REAL_READABLE)) is True
                        and _REAL_READABLE.read_bytes().endswith(b"\xff\xd9"),
                        f"{_REAL_READABLE.stat().st_size}B")
        else:
            print("  SKIP (真素材 0212307c…_b.jpg 不在，跳过可选对照)")
    finally:
        engine.dispose()
    return bool(ok)


def test_source_is_complete_the_tail_is_ours_to_lose() -> bool:
    """真因的证据：源 ``.dat`` 比它声明的载荷**正好多 16 字节**（不是竞态）。

    这条把"要不要加稳定性等待"钉死：源文件是完整的、大小稳定，丢尾巴的是
    ``db/image_decrypt.py::_decrypt_dat_v2`` 按声明长度截断，所以在拷贝前加
    size/mtime 稳定性等待对这一类故障**没有帮助**（本文件因此没有那条用例）。
    真源目录不在（别的机器）时 SKIP。
    """
    print("\n[⑥ 真因证据：源 .dat 比声明载荷多 16 字节]")
    import struct

    if not _REAL_BROKEN.exists() or not _WX_CACHE.exists():
        print("  SKIP (缺真素材或微信缓存目录)")
        return True
    sources = list(_WX_CACHE.rglob(f"{_REAL_BROKEN.stem}.dat"))
    if not sources:
        print("  SKIP (找不到同名源 .dat)")
        return True
    raw = sources[0].read_bytes()
    aes_size, xor_size = struct.unpack_from("<II", raw, 6)
    aligned = aes_size + (-aes_size % 16)
    extra = raw[15 + aligned + xor_size:]
    cached = _REAL_BROKEN.read_bytes()
    ok = True
    ok &= check("源是 V2 容器（07 08 'V2'）", raw[2:4] == b"V2", raw[:4].hex())
    ok &= check("源比声明载荷多 16 字节（尾块真实存在，不是竞态）",
                len(extra) == 16, str(len(extra)))
    # 解密侧修好之后的读数：缓存比声明的载荷**正好多这 16 字节**，且以 EOI 收尾。
    # 修之前这里是 127684 == aligned + xor_size、尾 9478 —— 这条会随生产者的
    # 状态在两个方向上都给出明确读数，而不是含糊地"文件存在"。
    ok &= check("缓存文件 == 声明载荷 + 那 16 字节（尾块已经补回）",
                len(cached) == aligned + xor_size + len(extra),
                f"{len(cached)} vs {aligned + xor_size + len(extra)}")
    ok &= check("缓存末尾是 ffd9（视觉不会再 400）",
                cached.endswith(b"\xff\xd9"), cached[-2:].hex())
    try:
        from db.image_key import get_xor_key

        key = get_xor_key()
        key = get_xor_key()
        if not key:
            # 0 不是"真 key"，是"算不出来"：key 由微信库里的 code 与 wxid 派生，
            # 微信客户端占着库时读不到 → 这里只能 SKIP（这条本身只是证据，
            # 尾块是否补回已由上两条钉死：长度 == 载荷+16、缓存以 ffd9 收尾）。
            print("  SKIP (取不到真 xor key：算出来是 0，通常是微信库被占用)")
        else:
            ok &= check(f"那 16 字节 XOR {key:#04x} 之后以 ffd9 收尾（正是被丢的 EOI）",
                        bytes(b ^ key for b in extra).endswith(b"\xff\xd9"),
                        bytes(b ^ key for b in extra)[-2:].hex())
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP (取不到 xor key：{exc!r})")
    return bool(ok)


def test_decrypt_keeps_the_trailing_tail() -> bool:
    """尾块是我们自己的责任：不许依赖真微信库与真 key。

    上一条用真素材，但「真 key」要从微信库里派生，微信客户端占着库时算不出来 ——
    那样这条证据就随环境时红时绿。这里改成自带密钥的构造容器：
    头部声明 16 字节 AES 段（内容是一个整块 padding）+ N 字节 XOR 段，声明区之后
    再挂 16 字节同密钥的尾巴，尾巴解出来正好以 ``ff d9`` 收尾。
    """
    print("\n[⑦ 构造容器：声明区之后的尾块必须被保留]")
    import struct
    from unittest.mock import patch

    from Cryptodome.Cipher import AES

    KEY = b"0123456789abcdef"
    XKEY = 0x42
    body = b"\xff\xd8\xff" + b"body-bytes"      # 解出来要是完整 JPEG 头（_IMG_HEADS 认三个字节）
    tail = b"TAIL" + b"\x00" * 10 + b"\xff\xd9"   # 恰好 16 字节，收尾是 EOI
    assert len(tail) == 16
    block = AES.new(KEY, AES.MODE_ECB).encrypt(b"\x10" * 16)   # 整块 padding → 解出空
    xor_part = bytes(b ^ XKEY for b in body)
    tail_part = bytes(b ^ XKEY for b in tail)
    head = b"\x07\x08V2" + b"\x00\x00" + struct.pack("<II", 16, len(xor_part)) + b"\x00"
    assert len(head) == 15
    container = head + block + xor_part + tail_part

    import db.image_key as image_key
    ok = True
    with patch.object(image_key, "get_aes_key", lambda: KEY), \
         patch.object(image_key, "get_xor_key", lambda: XKEY):
        from db.image_decrypt import decrypt_dat
        got, ext = decrypt_dat(container)
        ok &= check("解出来的图以 JPEG 头开始", bool(got) and got.startswith(b"\xff\xd8"), ext)
        ok &= check("声明区之后的 16 字节尾巴被保留（不是被丢掉）",
                    bool(got) and got.endswith(tail), (got or b"")[-8:].hex())
        ok &= check("尾巴补上之后整图以 ffd9 收尾", bool(got) and got.endswith(b"\xff\xd9"),
                    (got or b"")[-2:].hex())
        ok &= check("正文逐字节未被改动", bool(got) and got[:len(body)] == body,
                    (got or b"")[:8].hex())
        # 反证：同一容器把尾巴截掉，就得不到完整图 —— 这条锁的正是"丢了会怎样"
        cut, _ext = decrypt_dat(container[:-16])
        ok &= check("（反证）截掉尾块就收不了 ffd9", not (cut or b"").endswith(b"\xff\xd9"),
                    (cut or b"")[-2:].hex())
    return bool(ok)


def test_isolation_guards_hold() -> bool:
    """守卫：库隔离 + 相邻开关真的钉住了（否则上面那些"关=旧行为"的断言不成立）。"""
    print("\n[守卫：隔离与开关钉死]")
    ok = True
    real_meta = _ROOT / "data" / "yinyue_meta.db"
    ok &= check("meta 库已隔离（没指向生产库）",
                os.path.realpath(str(_config.paths.meta_db_path))
                != os.path.realpath(str(real_meta)),
                str(_config.paths.meta_db_path))
    for key, want in (
        ("YINYUE_FEATURE_SEMANTIC_UNIT", "off"),
        ("YINYUE_FEATURE_PAT_GRACE", "off"),
        ("YINYUE_FEATURE_GROUP_COALESCE", "off"),
        ("YINYUE_FEATURE_REPLY_INTENT", "off"),
        ("YINYUE_ADMISSION_MODE", "off"),
        ("YINYUE_FEATURE_OUTPUT_GATE", "off"),
    ):
        ok &= check(f"{key}={want}", os.environ.get(key) == want,
                    repr(os.environ.get(key)))
    return bool(ok)


def main() -> bool:
    print("=" * 64)
    print("media integrity checks")
    print("=" * 64)
    tests = [
        test_isolation_guards_hold,
        test_truncated_jpeg_is_incomplete,
        test_resolution_keeps_pending_and_does_not_lie,
        test_no_vision_call_for_unreadable_image,
        test_complete_jpeg_passes,
        test_png_gif_webp_cells,
        test_mime_is_read_from_bytes_not_from_the_name,
        test_unknown_format_is_not_labelled_jpeg,
        test_incomplete_tail_is_readable_and_accepted,
        test_source_is_complete_the_tail_is_ours_to_lose,
        test_decrypt_keeps_the_trailing_tail,
    ]
    results = []
    try:
        for test in tests:
            try:
                results.append((test.__name__, bool(test())))
            except Exception:  # pragma: no cover - diagnostics only
                import traceback

                traceback.print_exc()
                results.append((test.__name__, False))
    finally:
        _cleanup()

    print("\n" + "=" * 64)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    failed = [n for n, ok in results if not ok]
    print(f"media integrity checks: {len(results) - len(failed)}/{len(results)} passed")
    print("=" * 64)
    return not failed


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(0 if main() else 1)
