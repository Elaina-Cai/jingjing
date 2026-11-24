"""缓存判据 × 图片类型 × 临时目录入口：三处"撒谎/粘住"的收尾回归位（2026-10-02）。

件①  ``db/sync_service.py::SyncService._cache_image_dat`` 的粘性缓存短路
----------------------------------------------------------------------
缓存命中原本只问 ``cached.stat().st_size > 0``：一次截断的写入（就是"她看不见图"
那批缺 EOI/IEND 的缓存件）被永久当成"已缓存"，于是"以后再解一次"永远不会发生，
坏图一直被交给视觉供应商换回 ``400 unsupported image``。现在判定走
``core.conversation.media.is_complete_image`` —— 媒体完整性的**唯一 owner**，
本文件只调用、不改它的判据；坏图不算命中，允许后续重试；完整图照旧命中
（缓存不许被打没）。

件②  ``llm/client.py`` 写死 ``data:image/jpeg``
---------------------------------------------
裸 base64 的图片被硬编码声明成 jpeg：PNG / WEBP / GIF 的字节与声明类型不一致，
供应商有权拒。类型现在只由 ``core.conversation.media.image_mime``（**magic-first**）
给出——``llm/`` 里不再有第二份 MIME 猜测，认不出格式就**不编造**。

件③  scripts/ 与 tests/ 的临时目录唯一入口
-----------------------------------------
结构守卫：``scripts/`` 与 ``tests/`` 里不许再有绕过 ``tests/_tempdirs.py`` 的
``gettempdir()`` / ``mkdtemp()``。系统 TEMP 不可写时 ``gettempdir()`` 会静默回落到
仓库根（那轮 18 个测试 ``unable to open database file`` 的形态），所以脚本侧也收口到
同一个入口。

判据是 **AST 多行感知**的（2026-10-03 收口）：一个调用合规与否只看"有没有显式
``dir=``"，与它写在一行还是拆成三行无关——旧版按行扫，``mkstemp(`` 与 ``dir=``
分行就会被误判成裸用。多行感知**没有**放宽：裸 ``mkstemp()`` 照抓，而且
``dir=tempfile.gettempdir()`` 这类"显式裸用"现在也算违规（旧版只看字面在不在同一行，
反而放过了它）。白名单因此只剩"唯一入口本身"一条。

测试卫生
--------
件①②是纯缺陷修复，**没有**新开关：本文件不断言任何"关=旧行为"。唯一显式钉死的值
是 ``MEDIA_INTEGRITY=off`` 那一格，用来证明件①的缓存判据**不**受该开关管辖——
按本仓库的规矩，"关"必须显式写 ``off``，"env 未设"不等于关。

Run directly: python tests/test_media_cache_and_mime.py
"""

from __future__ import annotations

import ast
import asyncio
import base64
import inspect
import io
import json
import os
import shutil
import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# 临时目录的唯一入口（本文件同时是它的守卫位，见件③）。
sys.path.insert(0, str(ROOT / "tests"))

from _tempdirs import make_db_dir, temp_base  # noqa: E402

# ---------------------------------------------------------------------------
# 库隔离（照 tests/test_media_integrity.py）：必须在 import core.* 之前设好，
# 否则 core.config.settings 首次导入就把**生产**路径固化进来。本文件只读判据、
# 不写库，但 import 链会经 core.bootstrap —— 隔离是防呆，不是装饰。
# ---------------------------------------------------------------------------
_ISO_ROOT = ROOT / "data" / "_test_media_cache_mime_iso"
_ISO_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["META_DB_PATH"] = str(_ISO_ROOT / "meta.db")
os.environ["PLATFORMS_DIR"] = str(_ISO_ROOT / "platforms")
os.environ["DATA_DIR"] = str(_ISO_ROOT / "data")

from core.conversation.media import (  # noqa: E402
    image_integrity,
    image_mime,
    is_complete_image,
    media_path_to_data_uri,
)
from db.sync_service import SyncService  # noqa: E402
from llm.client import LLMClient  # noqa: E402

_FAILURES: list[str] = []

#: 素材落点：唯一入口的基座下（不进仓库、不进系统 TEMP）。
_IMG_DIR = temp_base() / "media_cache_mime_imgs"


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✓' if ok else '✗'} {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        _FAILURES.append(label)
    return bool(ok)


@contextmanager
def _switch(value: str):
    """Pin one switch for a case; explicit ``off`` is written, never implied."""
    key = "YINYUE_FEATURE_MEDIA_INTEGRITY"
    saved = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


# ---------------------------------------------------------------------------
# 素材：现场构造，不提交任何二进制
# ---------------------------------------------------------------------------

def _gradient(kind: str, size: int = 48) -> bytes:
    """A small but real image of *kind* (size changes the pixels, hence the bytes)."""
    from PIL import Image

    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            pixels[x, y] = (
                (x * 3 + size) % 256, (y * 5 + size) % 256, (x + y + size) % 256,
            )
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


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# 件①：缓存命中要有完整性的判据
# ---------------------------------------------------------------------------

def test_truncated_cache_entry_is_not_a_hit() -> bool:
    """① 截断的缓存件不算命中（重试发生）② 完整的缓存件仍然命中（缓存没被打没）。"""
    print("\n[件① 缓存命中：完整才算命中]")
    ok = True
    workdir = make_db_dir("media_cache_")
    cwd = os.getcwd()
    try:
        cache_dir = workdir / "data" / "image_cache"
        src_dir = workdir / "srcs"
        cache_dir.mkdir(parents=True, exist_ok=True)
        src_dir.mkdir(parents=True, exist_ok=True)

        full = _gradient("jpg", 48)
        other = _gradient("jpg", 56)
        cut = full[: len(full) - 2]  # 缺 EOI：实测缓存里那一批的形态

        # ① 截断缓存件 + 完整源 → 不许命中，重取一次
        cached_cut = _write(cache_dir / "img_a.jpg", cut)
        src_a = _write(src_dir / "img_a.jpg", full)
        ok &= check("夹具契约：截断缓存件 size > 0（旧的 size>0 判据会命中它）",
                    cached_cut.stat().st_size > 0, f"{cached_cut.stat().st_size}B")
        ok &= check("夹具契约：截断缓存件不是完整图",
                    is_complete_image(str(cached_cut)) is False,
                    image_integrity(str(cached_cut)))

        os.chdir(workdir)
        out_a = SyncService._cache_image_dat(str(src_a))
        ok &= check("截断缓存件不算命中：交回的是重取后的完整字节",
                    Path(out_a).read_bytes() == full,
                    f"{len(Path(out_a).read_bytes())}B vs src {len(full)}B")
        ok &= check("截断缓存件不算命中：重取真的发生了（现在完整）",
                    is_complete_image(out_a) is True, image_integrity(out_a))

        # ② 完整缓存件 + 完整的、**不一样的**源 → 必须命中缓存自己（不是源的字节）
        cached_full = _write(cache_dir / "img_b.jpg", other)
        src_b = _write(src_dir / "img_b.jpg", full)
        ok &= check("夹具契约：缓存件与源字节不同（命中与否可分辨）", other != full)
        out_b = SyncService._cache_image_dat(str(src_b))
        ok &= check("完整缓存件仍然命中：交回缓存自己的字节（性能没崩）",
                    Path(out_b).read_bytes() == other, str(out_b))
        ok &= check("完整缓存件仍然命中：用的是缓存路径（不是重写的一份）",
                    Path(out_b).resolve() == cached_full.resolve(), str(out_b))

        # ③ 判据不归 MEDIA_INTEGRITY 开关管：显式 off 也一样（纯缺陷修复，无新开关）
        cached_cut2 = _write(cache_dir / "img_c.jpg", cut)
        src_c = _write(src_dir / "img_c.jpg", full)
        with _switch("off"):
            out_c = SyncService._cache_image_dat(str(src_c))
        ok &= check("MEDIA_INTEGRITY=off（显式）下也不把截断件当命中",
                    Path(out_c).read_bytes() == full, str(cached_cut2))
    finally:
        os.chdir(cwd)
        shutil.rmtree(workdir, ignore_errors=True)
    return bool(ok)


def test_cache_decision_uses_the_single_integrity_owner() -> bool:
    """结构锁：缓存判据只调用唯一 owner，本模块里没有第二份完整性判断。"""
    print("\n[件① 结构锁：判据只有一份实现]")
    ok = True
    function_code = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(SyncService._cache_image_dat)
    )))
    module_code = ast.unparse(ast.parse(
        (ROOT / "db" / "sync_service.py").read_text(encoding="utf-8-sig")
    ))
    ok &= check("_cache_image_dat 走 is_complete_image（唯一 owner）",
                "is_complete_image(" in function_code)
    ok &= check("_cache_image_dat 里没有第二份完整性判断（无 size/魔数/尾标记）",
                not any(token in function_code for token in
                        ("st_size", "getsize", "\\xff\\xd8", "\\xff\\xd9",
                         "IEND", "GIF8")),
                "命中判定不再由文件大小或自带魔数给出")
    ok &= check("本模块没有自定义的完整性判据函数",
                not any(token in module_code for token in
                        ("def is_complete_image", "def image_integrity",
                         "def _is_complete", "def _looks_like_image")))
    ok &= check("本模块没有任何图片魔数（判据不在这里复制）",
                not any(token in module_code for token in
                        ("\\xff\\xd8", "\\xff\\xd9", "\\x89PNG", "GIF8", "IEND")))
    return bool(ok)


# ---------------------------------------------------------------------------
# 件②：图片类型只由 image_mime 给出
# ---------------------------------------------------------------------------

class _Completions:
    """The one provider call ``chat_vision`` makes, captured instead of sent."""

    def __init__(self, captured: list[dict]) -> None:
        self._captured = captured

    async def create(self, **kwargs):
        self._captured.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok", reasoning_content=""),
            )],
            usage=SimpleNamespace(total_tokens=11),
        )


class _ProviderClient:
    def __init__(self, captured: list[dict]) -> None:
        self.chat = SimpleNamespace(completions=_Completions(captured))


def _vision_call(images: list[str]) -> tuple[list[dict], str]:
    """Drive the real ``chat_vision`` with a stub provider; return (payloads, reply)."""
    captured: list[dict] = []
    client = LLMClient()
    # Provider selection is not what this file tests: pin one stub provider.
    client._filter_usable_providers = (  # type: ignore[assignment]
        lambda chain, vision_mode=False: (["stub"], [])
    )
    client._get_model = lambda provider: "stub-model"  # type: ignore[assignment]
    client._get_client = lambda provider: _ProviderClient(captured)  # type: ignore[assignment]
    reply = asyncio.run(client.chat_vision(
        system="system", user="user", images=list(images), agent_name="vision_test",
    ))
    return captured, reply


def _image_urls(payload: dict) -> list[str]:
    parts = payload["messages"][1]["content"]
    return [
        part["image_url"]["url"] for part in parts
        if part.get("type") == "image_url"
    ]


def test_bare_base64_keeps_its_real_mime() -> bool:
    """① PNG / GIF / WEBP / JPEG 各自得到正确 MIME（magic-first，不看名字）。"""
    print("\n[件② 裸 base64：类型来自字节]")
    ok = True
    for kind, want in (("png", "image/png"), ("gif", "image/gif"),
                       ("webp", "image/webp"), ("jpg", "image/jpeg")):
        raw = _gradient(kind)
        path = _write(_IMG_DIR / f"full.{kind}", raw)
        payload = base64.b64encode(raw).decode("ascii")
        captured, reply = _vision_call([payload])
        urls = _image_urls(captured[-1])
        ok &= check(f"{kind.upper()}: 发出 data:{want};base64,",
                    urls == [f"data:{want};base64,{payload}"],
                    urls[0][:34] if urls else "（没有 image_url）")
        ok &= check(f"{kind.upper()}: 与唯一 owner image_mime 一致",
                    image_mime(str(path)) == want, image_mime(str(path)))
        ok &= check(f"{kind.upper()}: 调用本身成功", reply == "ok", repr(reply))

    # 名字撒谎也一样：字节是 PNG，就叫 PNG（类型从来不是从名字读的）。
    liar = _write(_IMG_DIR / "actually_png.jpg", _gradient("png"))
    payload = base64.b64encode(liar.read_bytes()).decode("ascii")
    captured, _ = _vision_call([payload])
    ok &= check("PNG 伪装成 .jpg → 仍报 image/png",
                _image_urls(captured[-1]) == [f"data:image/png;base64,{payload}"])

    # 对照位：已经带类型的 data URI / URL 原样经过（不在 llm 层重新标注）。
    uri = media_path_to_data_uri(str(_IMG_DIR / "full.png"))
    captured, _ = _vision_call([uri, "https://example.invalid/a.png"])
    ok &= check("已带类型的 data URI 原样经过", _image_urls(captured[-1])[0] == uri)
    ok &= check("http(s) URL 原样经过",
                _image_urls(captured[-1])[1] == "https://example.invalid/a.png")
    return bool(ok)


def test_unknown_format_is_not_labelled_jpeg() -> bool:
    """② 认不出的载荷如实降级：不硬说 jpeg，也不把非 base64 包成 data URI。"""
    print("\n[件② 未知格式：不编造类型]")
    ok = True
    junk = base64.b64encode(b"NOT-AN-IMAGE-AT-ALL" * 24).decode("ascii")
    captured, reply = _vision_call([junk])
    payload = captured[-1]
    ok &= check("未知字节：一个 image_url 都不发", _image_urls(payload) == [],
                str(_image_urls(payload))[:40])
    ok &= check("未知字节：请求照旧发出（文本部分在，调用成功）",
                payload["messages"][1]["content"][0]["type"] == "text"
                and reply == "ok", repr(reply))
    ok &= check("未知字节：整个请求里没有出现 image/jpeg",
                "image/jpeg" not in json.dumps(payload, ensure_ascii=False))

    captured, _ = _vision_call(["[图片]", ""])
    ok &= check("占位符 / 空串：同样不发 image_url（以前会被包成 jpeg）",
                _image_urls(captured[-1]) == [])

    good = base64.b64encode(_gradient("png")).decode("ascii")
    captured, _ = _vision_call([junk, good])
    ok &= check("混合列表：认得出的留下、认不出的丢掉",
                _image_urls(captured[-1]) == [f"data:image/png;base64,{good}"],
                str(_image_urls(captured[-1]))[:40])
    return bool(ok)


def _code_strings(path: Path) -> list[str]:
    """Every string/bytes **constant** in *path*, docstrings excluded.

    Comments and prose may name the old literal (that is documentation); this
    scan is about decision logic, so only real constants count.
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docstrings.add(id(first.value))
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant)
                and isinstance(node.value, (str, bytes))
                and id(node) not in docstrings):
            value = node.value
            found.append(
                value.decode("latin-1") if isinstance(value, bytes) else str(value)
            )
    return found


def test_llm_layer_has_no_mime_guesswork() -> bool:
    """结构锁：``llm/`` 里没有第二份 MIME 猜测，类型只从 owner 来。"""
    print("\n[件② 结构锁：llm/ 里没有第二份猜测]")
    ok = True
    literals = ("image/jpeg", "image/png", "image/gif", "image/webp",
                "image/bmp", "image/tiff")
    magics = ("\xff\xd8", "\xff\xd9", "\x89PNG", "GIF8", "RIFF", "IEND")
    guessers = ("mimetypes", "imghdr", "guess_type")
    offenders: list[str] = []
    for path in sorted((ROOT / "llm").rglob("*.py")):
        for text in _code_strings(path):
            for token in literals + magics:
                if token in text:
                    offenders.append(f"{path.name}: {token!r} in {text[:28]!r}")
        code = ast.unparse(ast.parse(path.read_text(encoding="utf-8-sig")))
        for token in guessers:
            if token in code:
                offenders.append(f"{path.name}: {token}")
    ok &= check("llm/ 里没有 MIME 字面量 / 魔数 / 现成猜测器",
                not offenders, "; ".join(offenders[:4]))

    callers: list[Path] = []
    for path in sorted((ROOT / "llm").rglob("*.py")):
        code = ast.unparse(ast.parse(path.read_text(encoding="utf-8-sig")))
        if "image_mime(" in code:
            callers.append(path)
    ok &= check("llm/ 下只有一个文件调用 image_mime（唯一 owner 调用点）",
                len(callers) == 1, ", ".join(path.name for path in callers))
    if len(callers) == 1:
        code = ast.unparse(ast.parse(callers[0].read_text(encoding="utf-8-sig")))
        ok &= check(f"{callers[0].name} 从 core.conversation.media 取 image_mime",
                    "from core.conversation.media import" in code)
        ok &= check("写死的 data:image/jpeg;base64, 拼接已经不在了",
                    "image/jpeg;base64," not in code)
    return bool(ok)


# ---------------------------------------------------------------------------
# 件③：scripts/ 与 tests/ 的临时目录唯一入口
# ---------------------------------------------------------------------------

_TEMP_CALL_NAMES = frozenset({
    "gettempdir", "mkdtemp", "mkstemp", "NamedTemporaryFile",
    "SpooledTemporaryFile", "TemporaryDirectory",
})

#: 唯一入口自己的**诊断读数**：``system_temp_status()`` 里那次 ``gettempdir()`` 只为
#: 打印"系统 TEMP 为什么不可用"，不是临时落点（它不是 tempfile 的调用点，也没有
#: ``dir=`` 可言）。逐条给理由——这是本文件里**唯一**的豁免位。
_TEMP_DIAGNOSTIC = {
    "tests/_tempdirs.py": "唯一入口本身：gettempdir() 只用于 system_temp_status() 的诊断读数",
}

#: 危险 ``dir=``：拿系统临时目录当落点，等于把故障形态从"裸调用"换成"显式调用"。
_TEMP_UNSAFE_DIR_NAMES = _TEMP_CALL_NAMES | {"gettempdir"}


def _temp_call_violation(node: ast.Call) -> str | None:
    """*node* 为什么算"绕过唯一入口"，合规时返回 ``None``。

    多行感知（旧版按行扫，README 式换行写法会被误判）：只看 **AST**——
    keyword 参数在语法树里不分行，``mkstemp(\\n    dir=str(_ISO_ROOT),\\n)``
    与写在一行完全等价；行号只用于报错定位。

    判据（**不放宽**：裸用照旧抓，且比按行扫更严）：

    * 显式给了 ``dir=`` → 放行（落点由调用方选定，系统 TEMP 不参与）；
    * ``dir=`` 又是 ``gettempdir()`` / ``mkdtemp()`` 这类临时目录调用 → **照样算违规**
      （那是把故障形态从"裸调用"改成"显式调用"，不是收口）；
    * 没给 ``dir=`` → 违规（``tempfile`` 在系统 TEMP 不可写时静默回落到仓库根）；
    * 自己没给但**祖先调用**给了 → 违规（例如 ``Path(mkstemp()[1])`` 包一层：
      落点仍由系统 TEMP 决定）。
    """
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:  # pragma: no cover - defensive
        return None

    for keyword in node.keywords:
        if keyword.arg is None or keyword.arg != "dir":
            continue
        value = keyword.value
        inner = getattr(value, "func", None)
        inner_name = ""
        if isinstance(inner, ast.Name):
            inner_name = inner.id
        elif isinstance(inner, ast.Attribute):
            inner_name = inner.attr
        if inner_name in _TEMP_UNSAFE_DIR_NAMES:
            return (f"{name}() 的 dir= 又是 {inner_name}()：落点还是系统临时目录"
                    f"（TEMP 不可写时会静默回落进仓库根）")
        return None

    return (f"{name}() 没有显式 dir=：系统 TEMP 不可写时 tempfile 会静默回落到仓库根"
            f"（改用 tests/_tempdirs.py 的 temp_base() / make_db_dir()）")


def _temp_violations_from_source(source: str) -> list[tuple[str, int, str]]:
    """``(name, lineno, reason)``：源码里每一处绕过唯一入口的调用（AST，注释不算）。"""
    tree = ast.parse(source)
    found: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:  # pragma: no cover - defensive
            continue
        if name not in _TEMP_CALL_NAMES:
            continue
        reason = _temp_call_violation(node)
        if reason is not None:
            found.append((name, node.lineno, reason))
    return found


def _temp_violations(path: Path) -> list[tuple[str, int, str]]:
    """``_temp_violations_from_source`` 的文件版。"""
    return _temp_violations_from_source(path.read_text(encoding="utf-8-sig"))


#: 已收口到唯一入口的文件（脚本 → make_db_dir，测试 → temp_base）。
_TEMP_COLLECTED = (
    "scripts/backup_all.py",
    "scripts/restore_drill.py",
    "scripts/simulate_affect.py",
    "tests/test_affect.py",
    "tests/test_artifact_render.py",
    "tests/test_check_identity_keys.py",
    "tests/test_context_assembler_equivalence.py",
    "tests/test_context_user_prompt.py",
    "tests/test_declarative_tools.py",
    "tests/test_message_trim.py",
)


def test_temp_entry_is_the_single_entry() -> bool:
    """件③：``gettempdir()`` 回落进仓库根的形态，不许再从脚本/测试里长回来。"""
    print("\n[件③ scripts/ + tests/ 的临时目录唯一入口]")
    ok = True
    offenders: list[str] = []
    scanned = 0
    for base in ("scripts", "tests"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if any(part in rel for part in ("__pycache__", "_scratch", "golden/")):
                continue
            scanned += 1
            exempt = rel in _TEMP_DIAGNOSTIC
            for name, line, reason in _temp_violations(path):
                if exempt and name == "gettempdir":
                    continue
                offenders.append(f"{rel}:{line} {name}() — {reason}")
    ok &= check("scripts/ + tests/ 里没有绕过入口的临时目录调用",
                not offenders, "; ".join(offenders[:4]))
    ok &= check("扫描范围非空（守卫不是空转）", scanned > 50, str(scanned))

    # 多行感知的自证：**同一个**调用写两遍，只有换行不同——裸用照样抓、
    # ``dir=`` 换行照旧放行（旧的按行扫会把后者误判成裸用）。
    inline = _temp_violations_from_source(
        "import tempfile\nx = tempfile.mkstemp(dir=str(ROOT), prefix='a_')\n")
    wrapped = _temp_violations_from_source(
        "import tempfile\nx = tempfile.mkstemp(\n    dir=str(ROOT),\n    prefix='a_',\n)\n")
    bare = _temp_violations_from_source("import tempfile\nx = tempfile.mkstemp()\n")
    unsafe = _temp_violations_from_source(
        "import tempfile\nx = tempfile.mkstemp(dir=tempfile.gettempdir())\n")
    ok &= check("多行感知：dir= 换行写与同行写一样放行",
                not inline and not wrapped,
                f"同行 {inline} / 换行 {wrapped}")
    ok &= check("多行感知没有放宽：裸 mkstemp 仍然算违规",
                [name for name, _, _ in bare] == ["mkstemp"], str(bare))
    ok &= check("dir=gettempdir() 这种'显式裸用'照样算违规",
                any(name == "mkstemp" and "gettempdir" in reason
                    for name, _, reason in unsafe), str(unsafe))

    for rel in _TEMP_COLLECTED:
        source = (ROOT / rel).read_text(encoding="utf-8-sig")
        ok &= check(
            f"{rel} → tests/_tempdirs.py",
            "_tempdirs" in source and ("make_db_dir(" in source or "temp_base(" in source),
        )

    # 豁免位**只剩诊断那一条**：不再有任何文件靠"整文件豁免"绕开判据。
    # 每个仍带 ``dir=`` 的调用位（如 ``tests/test_addressee_guard.py`` 的 ``mkstemp``）
    # 由判据自己查"dir= 是不是显式给的"——名单少一条，覆盖面反而大一格。
    ok &= check(
        "豁免只剩 gettempdir() 的诊断读数（逐条带理由；没有一个文件被整份跳过）",
        set(_TEMP_DIAGNOSTIC) == {"tests/_tempdirs.py"},
        f"{sorted(_TEMP_DIAGNOSTIC)}；这张表**手工维护**，没有生成器也没有 --write 可刷："
        "新文件一律 `from _tempdirs import temp_base / make_db_dir`（落点由唯一隔离目录决定），"
        "根本不需要 `dir=tempfile.gettempdir()`。只有「只读系统 TEMP 状态、不落盘」的诊断代码"
        "才配留在这里：往 `_TEMP_DIAGNOSTIC`（本文件顶部那张表）加一行 `<相对路径>: \"<为什么只能读、"
        "为什么不是落点>\"`，并确认该文件确有对应调用位（下面那条「对应真实调用位」自证会核 `def "
        "system_temp_status` 且只读不写）。想看全仓实况：跑 `python "
        "tests\\test_media_cache_and_mime.py`，本条的 offenders 会列前 4 处。",
    )

    # 诊断豁免不是空转：``tests/_tempdirs.py`` 里确实有一处 gettempdir() 在读它，而
    # 那一处是**诊断**（system_temp_status）而不是落点。
    entry = (ROOT / "tests" / "_tempdirs.py").read_text(encoding="utf-8-sig")
    ok &= check("诊断豁免对应真实调用位（system_temp_status 读 gettempdir）",
                "gettempdir()" in entry and "def system_temp_status" in entry)

    bad_dir: list[str] = []
    for base in ("scripts", "tests"):
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if any(part in rel for part in ("__pycache__", "_scratch", "golden/")):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name not in _TEMP_CALL_NAMES or name == "gettempdir":
                    continue
                if not any(kw.arg == "dir" for kw in node.keywords):
                    bad_dir.append(f"{rel}:{node.lineno} {name}()")
    ok &= check("每一处 mkstemp/mkdtemp/TemporaryDirectory 都显式给了 dir=，"
                "找不到一处没有 dir=（裸用会回落进仓库根）", not bad_dir,
                "; ".join(bad_dir[:4]))
    return bool(ok)


def main() -> bool:
    print("=" * 64)
    print("media cache decision + mime + temp entry checks")
    print("=" * 64)
    tests = [
        test_truncated_cache_entry_is_not_a_hit,
        test_cache_decision_uses_the_single_integrity_owner,
        test_bare_base64_keeps_its_real_mime,
        test_unknown_format_is_not_labelled_jpeg,
        test_llm_layer_has_no_mime_guesswork,
        test_temp_entry_is_the_single_entry,
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
    for name, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'} {name}")
    failed = [name for name, passed in results if not passed]
    print(f"media cache/mime checks: {len(results) - len(failed)}/{len(results)} passed")
    print("=" * 64)
    return not failed


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(0 if main() else 1)
