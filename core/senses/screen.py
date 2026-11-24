"""截图（只读；同一画面不重复落盘）。

落在 ``data/evidence/senses/<yyyymmdd>/<hhmmss>-<digest8>.png``：

- 为什么在 ``data/evidence/``：与 ``gui_click`` 的证据同一棵树，都是"我们做过/看过什么"的
  旁证，不进账本；
- 为什么按 ``digest`` 判重：桌面上什么都没变时连拍十张只会多占十份磁盘，画面一样就是同一张；
- 为什么 ``shadow`` 档**一张都不拍**：截图是落盘，是副作用；影子档只许判定。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from pathlib import Path

from core.senses.base import ScreenShot, SensesError

#: 默认落盘目录（相对仓库根）。
EVIDENCE_DIR = "data/evidence/senses"


def _now() -> datetime:
    return datetime.now()


def _grab_rgb() -> tuple[bytes, int, int]:
    """真截图：返回（RGB 原始字节, 宽, 高）。测试会替换这个函数。"""
    from PIL import ImageGrab

    image = ImageGrab.grab()
    rgb = image.convert("RGB")
    return (rgb.tobytes(), int(rgb.width), int(rgb.height))


def capture(
    *,
    out_dir: str | Path = EVIDENCE_DIR,
    grab=None,
    taken_at: datetime | None = None,
    write: bool = True,
) -> ScreenShot:
    """截一张。``grab`` 可注入（测试用）；``write=False`` 时只算指纹不落盘。"""
    fn = grab or _grab_rgb
    try:
        raw, width, height = fn()
    except SensesError:
        raise
    except Exception as exc:  # noqa: BLE001 - 截不到屏（无桌面/被挡住）就是读不到
        raise SensesError("unsupported", f"截不到屏：{type(exc).__name__}: {exc}") from exc
    if not raw or not width or not height:
        raise SensesError("failed", "截图返回空画面")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    when = taken_at or _now()
    shot = ScreenShot(
        path="",
        width=int(width),
        height=int(height),
        digest=digest,
        taken_at=when.isoformat(sep=" ", timespec="seconds"),
    )
    if not write:
        return shot
    directory = Path(out_dir) / when.strftime("%Y%m%d")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{when.strftime('%H%M%S')}-{digest[:8]}.png"
        if not target.exists():
            from PIL import Image

            image = Image.frombytes("RGB", (int(width), int(height)), raw)
            tmp = target.with_name("%s.%s.png" % (target.stem, uuid.uuid4().hex[:8]))
            image.save(tmp, format="PNG")
            tmp.replace(target)
    except Exception as exc:  # noqa: BLE001 - 落盘失败只丢证据，不影响"看见了"
        raise SensesError("failed", f"截图落盘失败：{type(exc).__name__}: {exc}") from exc
    return ScreenShot(
        path=str(target),
        width=int(width),
        height=int(height),
        digest=digest,
        taken_at=shot.taken_at,
    )
