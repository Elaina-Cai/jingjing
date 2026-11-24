"""语音合成：一句话 -> 一个音频文件。

2026-10-26 P4：只做合成，不做发送。发语音条要走 `OutboundGateway`（和文字同一条纪律：
静默、冷却、主动限流都要过），所以这里刻意不碰任何发送路径。

两条引擎，按可靠性排序自动降级：

1. **edge_tts**（首选）：音色自然、可调语速音高。但它是微软的在线服务，2026-10-26
   实测本机返回 `403 Invalid response status`（`Sec-MS-GEC` 令牌校验失败，本质是
   系统时间与微软服务器差太多）。403 不该让功能缺口，所以——
2. **SAPI**（兜底）：Windows 自带 `System.Speech`，完全离线，中文机上有
   `Microsoft Huihui Desktop`（zh-CN）。音色不如 edge 自然，但**永远能出声**。

纪律：
1. 永不抛：网络断了、语音包坏了，返回 None，调用方照常发文字。
2. 有硬超时：单独一个引擎超时就换下一个，绝不卡住这一轮。
3. 按内容 md5 命名，天然去重；显式给了 out_path 就按给的写。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import threading
from pathlib import Path

from safelog import warn

DEFAULT_VOICE = "zh-CN-XiaoyiNeural"
DEFAULT_RATE = "+0%"
DEFAULT_PITCH = "+0Hz"
DEFAULT_SAPI_VOICE = "Microsoft Huihui Desktop"
SYNTH_BUDGET_S = 20.0
ENGINES = ("cosyvoice3", "edge", "sapi")
_OUT_DIR = Path("data/tts_cache")


def available(engine: str = "") -> bool:
    """引擎装没装 / 能不能用（没装就安静地不发语音）。"""
    name = str(engine or "edge").lower()
    if name == "sapi":
        return os.name == "nt"
    if name == "cosyvoice3":
        # 飞鱼音色：模型在 WSL，Windows 侧只判断权重齐不齐、wsl.exe 在不在
        try:
            from core.speech import tts_cosyvoice

            return tts_cosyvoice.available()
        except Exception:
            return False
    try:
        import edge_tts  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _key(text: str, engine: str, voice: str, rate: str, pitch: str) -> str:
    raw = "\x00".join((text, engine, voice, rate, pitch)).encode("utf-8")
    return hashlib.md5(raw).hexdigest()[:16]


def _target(text: str, engine: str, voice: str, rate: str, pitch: str, out_path) -> Path:
    if out_path:
        return Path(out_path)
    suffix = ".mp3" if engine == "edge" else ".wav"
    return _OUT_DIR / (_key(text, engine, voice, rate, pitch) + suffix)


def _run_thread(coro, timeout_s: float) -> dict:
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["ok"] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            box["err"] = repr(exc)

    th = threading.Thread(target=_worker, name="tts-synth", daemon=True)
    th.start()
    th.join(timeout_s)
    if th.is_alive():
        box.setdefault("err", "timeout")
    return box


def _synth_edge(text: str, *, voice: str, rate: str, pitch: str, target: Path,
                budget_s: float) -> Path | None:
    try:
        import edge_tts  # noqa: PLC0415

        async def _go() -> str:
            communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            await communicate.save(str(target))
            return str(target)

        box = _run_thread(_go(), max(5.0, budget_s))
        if box.get("err"):
            warn("core/speech/tts_engine.py edge failed", err=str(box["err"])[:200])
            return None
        if not target.exists() or target.stat().st_size <= 0:
            warn("core/speech/tts_engine.py edge empty", path=str(target))
            return None
        return target
    except Exception as exc:  # noqa: BLE001
        warn("core/speech/tts_engine.py edge crashed", err=repr(exc)[:200])
        return None


def _rate_to_sapi(rate: str) -> int:
    """edge 的 `+20%` 换算成 SAPI 的 -10..10。"""
    try:
        text = str(rate or "").strip().rstrip("%")
        value = float(text.replace("+", "")) / 10.0
        return max(-10, min(10, int(round(value))))
    except Exception:  # noqa: BLE001
        return 0


_PS = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "try {{ $s.SelectVoice('{voice}') }} catch {{ }}; "
    "$s.Rate = {rate}; "
    "$s.SetOutputToWaveFile('{path}'); "
    "$s.Speak({text}); "
    "$s.Dispose()"
)


def _synth_sapi(text: str, *, voice: str, rate: str, target: Path, budget_s: float) -> Path | None:
    """Windows 自带语音（离线兜底）。"""
    try:
        if os.name != "nt":
            return None
        safe_voice = (voice or DEFAULT_SAPI_VOICE).replace("'", "")
        safe_text = text.replace("'", "’").replace("\n", " ")
        script = _PS.format(voice=safe_voice, rate=_rate_to_sapi(rate),
                            path=str(target).replace("'", "''"), text="'" + safe_text + "'")
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=max(5.0, budget_s))
        if proc.returncode != 0:
            warn("core/speech/tts_engine.py sapi failed", err=(proc.stderr or "")[:200])
            return None
        if not target.exists() or target.stat().st_size <= 0:
            warn("core/speech/tts_engine.py sapi empty", path=str(target))
            return None
        return target
    except Exception as exc:  # noqa: BLE001
        warn("core/speech/tts_engine.py sapi crashed", err=repr(exc)[:200])
        return None


CV3_BUDGET_S = 300.0


def _synth_cosyvoice(text: str, *, out_path: str | Path = "", style: str = "cute",
                     budget_s: float = CV3_BUDGET_S) -> Path | None:
    """飞鱼音色（CosyVoice3）：真正的合成在 WSL 的常驻服务里，这里等 wav 回来。

    首次会拉起服务（模型加载 60–80 秒），之后每句只是一次 socket 往返。
    权重不齐 / WSL 不可用 / 服务起不来，一律返回 None，让调用方降级到 edge / sapi。
    """
    name = str(style or "cute").strip().lower()
    if name not in ("cute", "gentle"):
        name = "cute"
    try:
        from core.speech import tts_cosyvoice
    except Exception as exc:
        warn("cosyvoice_import_failed", error="%s: %s" % (type(exc).__name__, str(exc)[:200]))
        return None
    try:
        return tts_cosyvoice.synth(text, style=name, out_path=out_path,
                                   budget_s=max(float(budget_s or 0.0), CV3_BUDGET_S))
    except Exception as exc:
        warn("cosyvoice_synth_failed", error="%s: %s" % (type(exc).__name__, str(exc)[:200]))
        return None


_LAST_ENGINE = {"name": ""}


def last_engine() -> str:
    """上一次真正出声的引擎（edge 挂了降级到 sapi 时，账本必须记 sapi，不能记 edge）。"""
    return str(_LAST_ENGINE.get("name") or "")


def synth_ex(
    text: str,
    *,
    engine: str = "",
    voice: str = "",
    rate: str = "",
    pitch: str = "",
    sapi_voice: str = "",
    out_path: str | Path = "",
    budget_s: float = SYNTH_BUDGET_S,
) -> tuple:
    """合成一句音频，返回 (路径, 真正用上的引擎)；失败返回 (None, "")。

    `engine` 指定首选引擎（cosyvoice3 / edge / sapi）；不指定就按 ENGINES 顺序自动降级。
    """
    _LAST_ENGINE["name"] = ""
    payload = str(text or "").strip()
    if not payload:
        return None, ""
    order: list[str] = []
    for name in (str(engine or "").lower(),) + ENGINES:
        if name and name in ENGINES and name not in order:
            order.append(name)
    hops: list[str] = []
    for name in order:
        if not available(name):
            hops.append(name + ":unavailable")
            continue
        if name == "cosyvoice3":
            got = _synth_cosyvoice(payload, out_path=out_path,
                                   style=voice or "cute", budget_s=budget_s)
            if got is not None:
                _LAST_ENGINE["name"] = name
                return got, name
            continue
        if name == "edge":
            target = _target(payload, name, voice or DEFAULT_VOICE, rate or DEFAULT_RATE,
                             pitch or DEFAULT_PITCH, out_path)
            if target.exists() and target.stat().st_size > 0 and not out_path:
                _LAST_ENGINE["name"] = name
                return target, name
            target.parent.mkdir(parents=True, exist_ok=True)
            got = _synth_edge(payload, voice=voice or DEFAULT_VOICE,
                              rate=rate or DEFAULT_RATE, pitch=pitch or DEFAULT_PITCH,
                              target=target, budget_s=budget_s)
        else:
            target = _target(payload, name, sapi_voice or DEFAULT_SAPI_VOICE, rate, "", out_path)
            if target.exists() and target.stat().st_size > 0 and not out_path:
                _LAST_ENGINE["name"] = name
                return target, name
            target.parent.mkdir(parents=True, exist_ok=True)
            got = _synth_sapi(payload, voice=sapi_voice or DEFAULT_SAPI_VOICE,
                              rate=rate, target=target, budget_s=budget_s)
        if got is not None:
            _LAST_ENGINE["name"] = name
            return got, name
        hops.append(name + ":no_wav")
    warn("core/speech/tts_engine.py synth failed",
         hops="|".join(hops) or "no_engine", chars=len(payload), want=str(engine or ""))
    return None, ""


def synth(
    text: str,
    *,
    engine: str = "",
    voice: str = "",
    rate: str = "",
    pitch: str = "",
    sapi_voice: str = "",
    out_path: str | Path = "",
    budget_s: float = SYNTH_BUDGET_S,
) -> Path | None:
    """老签名，只返回路径；要引擎名请用 synth_ex。"""
    return synth_ex(text, engine=engine, voice=voice, rate=rate, pitch=pitch,
                    sapi_voice=sapi_voice, out_path=out_path, budget_s=budget_s)[0]
