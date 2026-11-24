# -*- coding: utf-8 -*-
"""V2 ``.dat`` 尾巴守卫（2026-10-02「她看不见图」的**唯一**回归位）。

真因（Lead 定论，本文件是它的回归位）
-------------------------------------
微信图片容器的 V2 布局是::

    15 字节头（``data[2:4] == b"V2"``，偏移 6 处 ``<II`` = aes_size, xor_size）
    + 按 16 对齐的 AES-ECB 密文（解出图像前段 + PKCS7 填充）
    + XOR 区（``xor_size`` 字节）
    + **16 字节尾部**（同样是 XOR 加密，解出来正是 JPEG 的结束标记 ``ff d9``）

头里声明的 ``aes_size + xor_size`` **不是文件全部**：实测源 ``.dat`` 一律比声明区
多 16 字节（``python tests/test_media_integrity.py`` 的 [⑥] 那格是真素材锚）。
``db/image_decrypt.py::_decrypt_dat_v2`` 旧实现只取声明区、把这 16 字节丢掉，于是每张图
都缺 ``ff d9``：Pillow 解不开 / DeepSeek 视觉判 "unsupported image" → 她只能说
"图片没读出来"。**源 ``.dat`` 一直完整**（缓存件与解密输出 md5 相同 → 丢尾就发生在解密
这一步）。修复 = 把声明区之后的那段一并 XOR 消费后拼上。

本文件钉住的格子
----------------
① 合成 V2 ``.dat``（**不依赖生产大文件**）→ 解密结果**包含尾部**并以 ``ff d9`` 收尾；
② **变异自检**：把尾巴丢弃（= 修复前的口径，喂一条没有尾字节的同款 ``.dat``）→
   同一个守卫断言**必须失败**，证明用例真的在守这条而不是恒真；
③ 真 JPEG 往返：用 Pillow 现场造一张小图当载荷 → 解密输出与**原始图片逐字节相同**、
   Pillow 能完整加载（"她看得见图"的直接读数）；
④ 边界：声明 ``xor_size == 0`` 时尾巴照样消费（修复用的是"声明区之后"这一段，
   不是"再读一次 xor 区"）。

密钥**打桩**（``db.image_key`` 的两个读点换成固定测试密钥）：本文件既不读生产
``.dat``，也不依赖本机的微信密钥；打桩本身有一条"守卫的守卫"——
换一把 key 结论必须变，否则说明用例根本没走 XOR 那条路。

Run directly: python tests/test_image_decrypt_tail.py
"""

from __future__ import annotations

import io
import os
import struct
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# 隔离前导（照 tests/test_media_integrity.py:54-65）：本文件不写任何库，但绝不
# 让 import 期把**生产**路径固化进来（settings 首次导入即固化，之后改不回来）。
# ---------------------------------------------------------------------------
# 开关卫生：本文件**不依赖任何 feature 开关**（``db/image_decrypt.py`` 只读
# ``db/image_key.py`` 的密钥，不读 ``core/features.py``），所以这里没有"显式 off"的
# 断言、也不需要在 import 期钉死开关；同进程其它文件对开关的临时改动影响不到本文件
# （唯一的隐藏输入是密钥，已被 ``_keys()`` 打桩固定）。
_ROOT = Path(__file__).resolve().parent.parent
_ISO_ROOT = _ROOT / "data" / "_test_image_decrypt_tail_iso"
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("META_DB_PATH", str(_ISO_ROOT / "meta.db"))
os.environ.setdefault("PLATFORMS_DIR", str(_ISO_ROOT / "platforms"))
os.environ.setdefault("DATA_DIR", str(_ISO_ROOT / "data"))
os.environ.setdefault("LOG_DIR", str(_ISO_ROOT / "logs"))

from db.image_decrypt import decrypt_dat  # noqa: E402

#: 固定测试密钥（**不是**生产密钥）：AES = 16 字节 ascii；XOR = 生产实测的那把 0x4b。
AES_KEY = b"0123456789abcdef"
XOR_KEY = 0x4B

#: V2 头的公开形状：``07 08 'V2'`` + 15 字节总长，偏移 6 处两个小端 int。
_MAGIC = b"\x07\x08V2"
_HEADER_LEN = 15

#: 以 JPEG 魔数开头的小前缀（长度**故意**不是 16 的倍数 → PKCS7 填充必须真发生）。
_JPEG_PREFIX = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x11" * 6

#: 尾部 16 字节的明文形态：任意字节 + JPEG 结束标记 ``ff d9``。
_TAIL_PLAIN = b"\x5a" * 14 + b"\xff\xd9"

_FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        _FAILURES.append(label)
    return bool(ok)


# ---------------------------------------------------------------------------
# 密钥打桩 + 夹具构造
# ---------------------------------------------------------------------------

@contextmanager
def _keys(aes: bytes = AES_KEY, xor: int = XOR_KEY):
    """把 ``db.image_key`` 的两个读点换成固定测试密钥（``_decrypt_dat_v2`` 在调用时
    ``from db.image_key import ...``，所以换模块属性就够，不需要碰生产密钥）。"""
    from db import image_key

    old_aes, old_xor = image_key.get_aes_key, image_key.get_xor_key
    image_key.get_aes_key = lambda: aes  # type: ignore[assignment]
    image_key.get_xor_key = lambda: xor  # type: ignore[assignment]
    try:
        yield
    finally:
        image_key.get_aes_key = old_aes  # type: ignore[assignment]
        image_key.get_xor_key = old_xor  # type: ignore[assignment]


def _pkcs7(data: bytes, block: int = 16) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def _build_dat(
    *,
    aes_plain: bytes,
    xor_plain: bytes,
    tail_plain: bytes,
    declare_aes: int | None = None,
    aes_key: bytes = AES_KEY,
    xor_key: int = XOR_KEY,
    with_tail: bool = True,
) -> bytes:
    """按 V2 形状拼一条 ``.dat``（``with_tail=False`` = 修复前的文件口径）。

    ``declare_aes`` 给一个**非 16 倍数**的值可以顺带压到"声明长度按 16 对齐"那条分支；
    默认按真实形态声明填充后的密文长度（实测生产 ``aes_size`` = 1024，本身已对齐）。
    """
    from Cryptodome.Cipher import AES

    padded = _pkcs7(aes_plain)
    assert len(padded) % 16 == 0, "AES 区必须按 16 对齐"
    aes_size = len(padded) if declare_aes is None else declare_aes

    header = bytearray(_HEADER_LEN)
    header[0:4] = _MAGIC
    struct.pack_into("<II", header, 6, aes_size, len(xor_plain))

    cipher = AES.new(aes_key[:16], AES.MODE_ECB)
    body = cipher.encrypt(padded)
    body += bytes(b ^ xor_key for b in xor_plain)
    if with_tail:
        body += bytes(b ^ xor_key for b in tail_plain)
    return bytes(header) + body


def _declared_payload(dat: bytes) -> bytes:
    """头里声明的 ``aligned_aes_size + xor_size`` 那一段有多长（解剖用）。"""
    aes_size, xor_size = struct.unpack_from("<II", dat, 6)
    aligned = aes_size + (-aes_size % 16)
    return dat[_HEADER_LEN:_HEADER_LEN + aligned + xor_size]


def _tail_of(dat: bytes) -> bytes:
    """声明区**之后**的那一段（= 修复前被丢掉的部分）。"""
    declared = len(_declared_payload(dat))
    return dat[_HEADER_LEN + declared:]


def _tail_guard(image: bytes | None, expected: bytes) -> bool:
    """本文件要守的那条断言：解出来的是**完整**图片（含 EOI），且逐字节等于原图。

    变异时（尾巴被丢）它必须返回 ``False`` —— 用例②就是在证这一点。
    """
    if image is None:
        return False
    return image == expected and image.endswith(b"\xff\xd9")


def _gradient_jpeg(size: int = 64) -> bytes:
    """现场造一张真 JPEG（几百~几千字节，不提交任何二进制素材）。"""
    from PIL import Image

    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            pixels[x, y] = (x * 4 % 256, y * 4 % 256, (x + y) % 256)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def _can_load(data: bytes) -> bool:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# ⓞ 夹具自检：合成件真的是"V2 + 尾巴"的形状
# ---------------------------------------------------------------------------

def test_fixture_contract() -> bool:
    """夹具自检：头是 V2、AES 区按 16 对齐、尾巴**正好 16 字节**且 XOR 后是 ``ff d9``。

    没有这条，后面所有"尾巴被吃掉了"的断言都可能跑在一个根本不是 V2 形状的夹具上
    （那样 ``decrypt_dat`` 会走裸 XOR 那条路，用例变成恒真）。
    """
    print("\n[ⓞ 夹具自检：合成 .dat 的形状]")
    dat = _build_dat(aes_plain=_JPEG_PREFIX, xor_plain=b"\x00\x11\x22\x33\x44",
                     tail_plain=_TAIL_PLAIN)
    aes_size, xor_size = struct.unpack_from("<II", dat, 6)
    ok = True
    ok &= check("data[2:4] == b'V2'（否则不会走 _decrypt_dat_v2）",
                dat[2:4] == b"V2", dat[:4].hex())
    ok &= check("aes_size 按 16 对齐（= 填充后的密文长度）",
                aes_size % 16 == 0 and aes_size == len(_pkcs7(_JPEG_PREFIX)),
                f"aes_size={aes_size}")
    aligned = aes_size + (-aes_size % 16)
    ok &= check("文件比声明区正好多 16 字节（实测源 .dat 的形态）",
                len(dat) == _HEADER_LEN + aligned + xor_size + 16,
                f"{len(dat)} vs {_HEADER_LEN + aligned + xor_size + 16}")
    ok &= check("尾部正好 16 字节（实测生产的形态）", len(_tail_of(dat)) == 16,
                str(len(_tail_of(dat))))
    ok &= check("尾部的**最后两字节** XOR 后是 ff d9（被丢掉的那个 EOI）",
                bytes(b ^ XOR_KEY for b in _tail_of(dat)).endswith(b"\xff\xd9"),
                bytes(b ^ XOR_KEY for b in _tail_of(dat))[-2:].hex())
    ok &= check("声明区里没有 ff d9（所以只看声明区必然截断）",
                not _declared_payload(dat).endswith(b"\xff\xd9"),
                _declared_payload(dat)[-2:].hex())
    ok &= check("_JPEG_PREFIX 长度不是 16 的倍数（PKCS7 填充真的发生）",
                len(_JPEG_PREFIX) % 16 != 0, str(len(_JPEG_PREFIX)))
    return bool(ok)


# ---------------------------------------------------------------------------
# ① 尾巴被消费 + ② 变异（丢掉尾巴）必须红
# ---------------------------------------------------------------------------

def test_tail_is_consumed_and_ends_with_eoi() -> bool:
    """★ 用例① + ②：合成 V2 ``.dat`` → 解出**完整**图片；丢掉尾巴 → 断言必须红。"""
    print("\n[① 尾巴被消费 → 以 ff d9 收尾]")
    aes_plain = _JPEG_PREFIX
    xor_plain = b"\x99\x88\x77\x66\x55"
    expected = aes_plain + xor_plain + _TAIL_PLAIN
    dat = _build_dat(aes_plain=aes_plain, xor_plain=xor_plain, tail_plain=_TAIL_PLAIN)

    ok = True
    with _keys():
        image, ext = decrypt_dat(dat)
        ok &= check("解密成功且认出 jpg", image is not None and ext == "jpg",
                    f"{None if image is None else len(image)}B {ext!r}")
        ok &= check("结果**包含尾部**（= 原图逐字节）", image == expected,
                    f"{None if image is None else len(image)}B vs {len(expected)}B")
        ok &= check("结果以 ff d9 收尾（她看得见图的直接读数）",
                    bool(image) and image.endswith(b"\xff\xd9"),
                    (image or b"")[-2:].hex())
        ok &= check("守卫 _tail_guard 在修复后的读数上成立",
                    _tail_guard(image, expected) is True)

        # ---- 变异：喂一条**没有尾字节**的同款 .dat（= 修复前的文件口径）----
        mutated_dat = _build_dat(aes_plain=aes_plain, xor_plain=xor_plain,
                                 tail_plain=_TAIL_PLAIN, with_tail=False)
        mutated, mutated_ext = decrypt_dat(mutated_dat)
        ok &= check("变异件确实比修复前少 16 字节",
                    len(dat) - len(mutated_dat) == 16, str(len(dat) - len(mutated_dat)))
        ok &= check("变异件解出来 == 声明区那段（= 修复前的唯一输出）",
                    mutated == aes_plain + xor_plain,
                    f"{None if mutated is None else len(mutated)}B")
        ok &= check("变异件 == 完整结果去掉尾部（同一段代码、只差这 16 字节）",
                    mutated == expected[:-len(_TAIL_PLAIN)])
        ok &= check("变异：不以 ff d9 收尾（这就是「她看不见图」）",
                    bool(mutated) and not mutated.endswith(b"\xff\xd9"),
                    (mutated or b"")[-2:].hex())
        ok &= check("★ 变异自检：同一个守卫断言在变异件上**必须失败**",
                    _tail_guard(mutated, expected) is False)
        ok &= check("变异件仍被认出 jpg（头部没坏 —— 只有尾巴被丢）",
                    mutated_ext == "jpg", mutated_ext)

        # ---- 守卫的守卫：换一把 XOR key，结论必须变 ----
        with _keys(xor=0x00):
            wrong, _ext = decrypt_dat(dat)
        ok &= check("守卫的守卫：XOR key 换掉后结果不再等于原图（XOR 那条路真的被走到）",
                    wrong != expected, f"{None if wrong is None else len(wrong)}B")
    return bool(ok)


def test_declared_aes_size_may_be_unaligned() -> bool:
    """声明 ``aes_size`` 非 16 倍数时按 16 对齐读 —— 尾巴照样不被丢。"""
    print("\n[①b 声明长度未对齐的分支 + 尾巴]")
    aes_plain = _JPEG_PREFIX
    xor_plain = b"\x01\x02\x03"
    expected = aes_plain + xor_plain + _TAIL_PLAIN
    dat = _build_dat(aes_plain=aes_plain, xor_plain=xor_plain, tail_plain=_TAIL_PLAIN,
                     declare_aes=len(aes_plain))  # 17 → 对齐到 32
    ok = True
    aes_size, xor_size = struct.unpack_from("<II", dat, 6)
    ok &= check("夹具确实是未对齐声明", aes_size % 16 != 0, f"aes_size={aes_size}")
    with _keys():
        image, ext = decrypt_dat(dat)
        ok &= check("未对齐声明 + 尾巴 → 仍是完整原图",
                    _tail_guard(image, expected) is True,
                    f"{None if image is None else len(image)}B {ext!r}")
    return bool(ok)


def test_tail_is_consumed_when_declared_xor_region_is_empty() -> bool:
    """边界：``xor_size == 0`` 时尾巴照样消费（修复读的是"声明区之后"，不是"再读一次"）。"""
    print("\n[④ 边界：xor_size == 0 也要吃到尾巴]")
    aes_plain = _JPEG_PREFIX
    expected = aes_plain + _TAIL_PLAIN
    dat = _build_dat(aes_plain=aes_plain, xor_plain=b"", tail_plain=_TAIL_PLAIN)
    _aes_size, xor_size = struct.unpack_from("<II", dat, 6)
    ok = True
    ok &= check("夹具的 xor_size == 0", xor_size == 0, str(xor_size))
    with _keys():
        image, _ext = decrypt_dat(dat)
        ok &= check("xor_size=0 + 尾巴 → 完整原图", _tail_guard(image, expected) is True,
                    f"{None if image is None else len(image)}B")
        mutated, _e = decrypt_dat(
            _build_dat(aes_plain=aes_plain, xor_plain=b"", tail_plain=_TAIL_PLAIN,
                       with_tail=False)
        )
        ok &= check("变异（xor_size=0 且丢尾）→ 守卫失败",
                    _tail_guard(mutated, expected) is False)
    return bool(ok)


# ---------------------------------------------------------------------------
# ③ 真 JPEG 往返：解密输出 == 原始图片（Pillow 能完整加载）
# ---------------------------------------------------------------------------

def test_real_jpeg_round_trips_byte_exact() -> bool:
    """★ 用例③：拿一张真 JPEG 当载荷 → 解密输出与**原始图片逐字节相同**。

    这是"她看得见图"的直接读数：Pillow 能 ``load()`` 完整像素，而丢掉尾巴的那 16 字节
    之后输出的就不再是原图（变异自检同样在这里再走一遍）。
    """
    print("\n[③ 真 JPEG 往返：解密输出 == 原图]")
    jpeg = _gradient_jpeg()
    ok = True
    ok &= check("素材是真 JPEG（以 ff d8 ff 开头、以 ff d9 收尾）",
                jpeg[:3] == b"\xff\xd8\xff" and jpeg.endswith(b"\xff\xd9"),
                f"{len(jpeg)}B {jpeg[-2:].hex()}")

    aes_len = ((len(jpeg) - 48) // 16) * 16        # 留出 ≥32B 的 XOR 区 + 16B 尾巴
    ok &= check("载荷够长，能切出 AES/XOR/尾巴三段", aes_len >= 16, str(aes_len))
    aes_plain, xor_plain, tail_plain = jpeg[:aes_len], jpeg[aes_len:-16], jpeg[-16:]
    ok &= check("切出来的尾巴正好 16 字节且以 ff d9 收尾",
                len(tail_plain) == 16 and tail_plain.endswith(b"\xff\xd9"),
                f"{len(tail_plain)}B {tail_plain[-2:].hex()}")

    dat = _build_dat(aes_plain=aes_plain, xor_plain=xor_plain, tail_plain=tail_plain)
    with _keys():
        image, ext = decrypt_dat(dat)
        ok &= check("解密输出与原图**逐字节相同**", image == jpeg,
                    f"{None if image is None else len(image)}B vs {len(jpeg)}B")
        ok &= check("Pillow 能完整加载解密输出（像素可读）",
                    image is not None and _can_load(image))
        ok &= check("输出以 ff d9 收尾", bool(image) and image.endswith(b"\xff\xd9"),
                    (image or b"")[-2:].hex())

        mutated, _e = decrypt_dat(
            _build_dat(aes_plain=aes_plain, xor_plain=xor_plain,
                       tail_plain=tail_plain, with_tail=False)
        )
        ok &= check("变异：丢尾 → 输出 != 原图 且不以 ff d9 收尾",
                    mutated is not None and mutated != jpeg
                    and not mutated.endswith(b"\xff\xd9"),
                    f"{None if mutated is None else len(mutated)}B")
        ok &= check("★ 变异自检：真图这一格的守卫断言同样必须失败",
                    _tail_guard(mutated, jpeg) is False)
    return bool(ok)


def test_isolation_guards_hold() -> bool:
    """守卫：本文件没把生产路径 / 生产密钥固化进来（否则上面的结论不成立）。"""
    print("\n[守卫：隔离与打桩]")
    from db import image_key

    ok = True
    ok &= check("META_DB_PATH 已隔离（没指向生产库）",
                os.path.realpath(str(os.environ.get("META_DB_PATH", "")))
                != os.path.realpath(str(_ROOT / "data" / "yinyue_meta.db")),
                str(os.environ.get("META_DB_PATH")))
    before = (image_key.get_aes_key, image_key.get_xor_key)
    with _keys():
        ok &= check("打桩中 get_aes_key == 固定测试密钥",
                    image_key.get_aes_key() == AES_KEY)
        ok &= check("打桩中 get_xor_key == 固定测试密钥", image_key.get_xor_key() == XOR_KEY)
    ok &= check("打桩退出后两个读点被还原（不污染同进程的其它用例）",
                (image_key.get_aes_key, image_key.get_xor_key) == before)
    return bool(ok)


def main() -> bool:
    print("=" * 64)
    print("image decrypt tail checks (V2 .dat: 声明区之后的 16 字节)")
    print("=" * 64)
    tests = [
        test_isolation_guards_hold,
        test_fixture_contract,
        test_tail_is_consumed_and_ends_with_eoi,
        test_declared_aes_size_may_be_unaligned,
        test_tail_is_consumed_when_declared_xor_region_is_empty,
        test_real_jpeg_round_trips_byte_exact,
    ]
    results = []
    for test in tests:
        try:
            results.append((test.__name__, bool(test())))
        except Exception:  # pragma: no cover - diagnostics only
            import traceback

            traceback.print_exc()
            results.append((test.__name__, False))

    print("\n" + "=" * 64)
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
    failed = [n for n, ok in results if not ok]
    print(f"image decrypt tail checks: {len(results) - len(failed)}/{len(results)} passed")
    print("=" * 64)
    return not failed


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(0 if main() else 1)
