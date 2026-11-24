"""飞鱼音色的 CosyVoice3 引擎（Windows 侧客户端）。

真正的合成跑在 WSL 里的常驻服务 scripts/cv3_server.py —— 模型只加载一次（60–80 秒），
之后每句话只是一次 socket 往返。这里只做三件事：确认环境在、进程没起就拉起来、
把一句话发过去等 wav 回来。任何一步失败都返回 None，调用方会自动降级到 edge / sapi。

开关与路径（都可以用环境变量改）：
  YINYUE_CV3_PORT      端口，默认 9881
  YINYUE_CV3_DISTRO    WSL 发行版，默认 Ubuntu
  YINYUE_CV3_PYTHON    WSL 里跑推理的解释器，默认 /home/elaina/cv3/.venv/bin/python
  YINYUE_CV3_REPO      WSL 里的 CosyVoice 源码目录，默认 /home/elaina/CosyVoice
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from core.infra.envflags import flag, text, text as _text

try:
    from structlog import get_logger

    log = get_logger(__name__)
except Exception:  # pragma: no cover - 无 structlog 时的兜底
    import logging

    log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_HOST = '127.0.0.1'
_PORT = int(_text("YINYUE_CV3_PORT") or 9881)
_DISTRO = _text("YINYUE_CV3_DISTRO") or 'Ubuntu'
_VENV_PY = _text("YINYUE_CV3_PYTHON") or '/home/elaina/cv3/.venv/bin/python'
_REPO = _text("YINYUE_CV3_REPO") or '/home/elaina/CosyVoice'
_SERVER = _ROOT / 'scripts' / 'cv3_server.py'
_LOG = _ROOT / 'data' / 'models' / 'cosyvoice3' / 'cv3_server.log'
CACHE_DIR = _ROOT / 'data' / 'tts_cache' / 'cv3'
BOOT_WAIT_S = 240.0
STYLES = ('cute', 'gentle')


from core.infra import egress, duration


def _fail(reason: str, detail: object = '') -> None:
    """失败只走这一条路：一个事件名 + 让她自己知道（CB-133）。

    reason 必须是 trouble.REASONS 里的键：分类写死在那张表里，调用点只选、不改。
    """
    log.warning('voice_tts_failed', service='cosyvoice', reason=reason, detail=str(detail)[:200])
    try:
        from core.infra import trouble

        trouble.note('voice', reason, str(detail)[:200])
    except Exception:  # noqa: silent(台账写不进去不该让一次合成失败变成崩溃)
        pass


def _recovered() -> None:
    """真的合成成功一次才算好了：删掉那条坏，否则她会长时间认为自己还坏着。"""
    try:
        from core.infra import trouble

        trouble.clear('voice')
    except Exception:  # noqa: silent
        pass


def pack_dir() -> Path:
    try:
        from core.assets import paths as _paths

        base = _paths.model_dir('cosyvoice3')
    except Exception:
        base = _ROOT / 'data' / 'models' / 'cosyvoice3'
    base = Path(base)
    if not base.is_absolute():
        # 2026-10-26 坑：model_dir() 默认返回相对路径，wsl_path() 见到没有盘符
        # 就原样透传，结果 WSL 侧收到 data\models\cosyvoice3\feiyu 这种路径，
        # AutoModel 当成模型仓库 id 去 ModelScope 拉包，报 404 E3020。
        base = _ROOT / base
    return (base / 'feiyu').resolve()


def model_files() -> dict[str, Path]:
    pack = pack_dir()
    base = pack / 'models' / 'Fun-CosyVoice3-0.5B-2512'
    return {
        'llm': base / 'llm.pt',
        'flow': base / 'flow.pt',
        'hift': base / 'hift.pt',
        'blanken': base / 'CosyVoice-BlankEN' / 'model.safetensors',
        'tokenizer': base / 'speech_tokenizer_v3.onnx',
        'campplus': base / 'campplus.onnx',
        'cute': pack / 'models' / 'feiyu_cute_F6_0040.pt',
        'ref_cute': pack / 'references' / 'cute_N01.wav',
        'ref_gentle': pack / 'references' / 'gentle_N10.wav',
    }


def missing_files() -> list[str]:
    return [name for name, path in model_files().items() if not Path(path).exists()]


def available() -> bool:
    if shutil.which('wsl.exe') is None:
        return False
    return not missing_files()


def wsl_path(path: str | Path) -> str:
    # 2026-10-26：已经写成 POSIX 的路径原样返回。原来无条件 Path() 再拼 _ROOT，
    # 而 Windows 上 "/home/x.wav" 不算绝对路径，会被拼成 C:\...\home\x.wav，
    # 再转成 /mnt/c/home/x.wav —— 一个 WSL 里本来能用的路径被改坏了。
    _raw = str(path)
    if _raw.startswith("/"):
        _text = _raw.replace("\\", "/")
        while "//" in _text:
            _text = _text.replace("//", "/")
        return _text
    p = Path(path)
    if not p.is_absolute():
        p = _ROOT / p
    text = str(p)
    if ':' not in text[:3]:
        return text
    drive = text[0].lower()
    rest = text[2:].replace('\\', '/')
    while '//' in rest:
        rest = rest.replace('//', '/')
    return '/mnt/%s/%s' % (drive, rest.lstrip('/'))


def _connect(timeout: float):
    return egress.connect(_HOST, _PORT, timeout_ms=duration.to_ms(f'{timeout}s'))


def _request(payload: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any] | None:
    try:
        sock = _connect(timeout)
    except OSError as exc:
        _fail('connect_refused', '%s: %s' % (type(exc).__name__, exc))
        return None
    try:
        body = (json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8')
        sock.settimeout(timeout)
        sock.sendall(body)
        buf = b''
        while not buf.endswith(b'\n'):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf.strip():
            return None
        return json.loads(buf.decode('utf-8', 'replace').strip().splitlines()[-1])
    except (TimeoutError, socket.timeout) as exc:
        _fail('synth_timeout', '%s: %s' % (type(exc).__name__, exc))
        return None
    except Exception as exc:  # 协议层的问题不该炸调用方
        _fail('server_error', '%s: %s' % (type(exc).__name__, exc))
        return None
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def ping(timeout: float = 1.5) -> bool:
    reply = _request({'op': 'ping'}, timeout=timeout)
    return bool(reply and reply.get('ok') and reply.get('ready'))


def _spawn_cmd() -> list[str]:
    """前台跑（不 nohup、不 `&`）：wsl.exe 自己活多久，服务就活多久。

    2026-10-26 实测：这台 WSL 没有 systemd，`nohup ... &` 在 wsl.exe 退出的瞬间
    被整机回收——进程 8 秒内消失、连 >> 日志都没建出来（代价是两轮 420 秒超时）。
    所以改成由 Windows 侧用 DETACHED_PROCESS 挂着这个 wsl.exe：
    只要它不退，发行版就不退，服务就在。
    """
    inner = 'cd %s && exec %s %s --port %d --pack %s' % (
        _REPO,
        _VENV_PY,
        wsl_path(_SERVER),
        _PORT,
        wsl_path(pack_dir()),
    )
    return ['wsl.exe', '-d', _DISTRO, '--', 'bash', '-lc', inner]


_PROC = {'popen': None, 'fh': None}


def _start_process() -> None:
    """detached 拉起 wsl.exe；父进程退出它也不死。"""
    fh = open(_LOG, 'ab', buffering=0)
    # 2026-10-26 实测：DETACHED_PROCESS 会让 wsl.exe 一声不响地什么都不做
    # （a/b/c 四种组合横评：带 DETACHED 的输出全空，CREATE_NO_WINDOW 正常）。
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    proc = subprocess.Popen(_spawn_cmd(), stdin=subprocess.DEVNULL, stdout=fh, stderr=fh,
                            creationflags=flags, close_fds=False)
    _PROC['popen'] = proc
    _PROC['fh'] = fh


_OFF_VALUES = {'0', 'off', 'false', 'no', 'none', 'disable', 'disabled'}


def autostart_allowed() -> bool:
    """允许自动拉起服务吗：测试、只想读缓存、手工起服务时设 YINYUE_CV3_AUTOSTART=off。"""
    return flag("YINYUE_CV3_AUTOSTART", default=True)


def start_server(wait_s: float = BOOT_WAIT_S) -> bool:
    if ping():
        return True
    if not autostart_allowed():
        log.info('cv3_autostart_off')
        _fail('autostart_off')
        return False
    if not available():
        _fail('not_installed', ','.join(missing_files()))
        return False
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        _start_process()
        log.info('cv3_server_spawned', port=_PORT, distro=_DISTRO)
    except Exception as exc:
        _fail('spawn_failed', '%s: %s' % (type(exc).__name__, exc))
        return False
    deadline = time.time() + max(5.0, wait_s)
    while time.time() < deadline:
        if ping(timeout=2.0):
            log.info('cv3_server_ready', waited_s=round(wait_s - (deadline - time.time()), 1))
            return True
        time.sleep(2.0)
    _fail('boot_timeout', 'waited %ss' % round(wait_s, 1))
    return False


def stop_server() -> bool:
    """先请服务自己退，再兜底掐掉挂着的 wsl.exe。"""
    reply = _request({'op': 'shutdown'}, timeout=5.0)
    proc = _PROC.get('popen')
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    fh = _PROC.get('fh')
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass
    _PROC['popen'] = None
    _PROC['fh'] = None
    return bool(reply and reply.get('ok'))


def status() -> dict[str, Any]:
    return {
        'available': available(),
        'missing': missing_files(),
        'pack': str(pack_dir()),
        'server_running': ping(),
        'port': _PORT,
        'distro': _DISTRO,
    }


def _fingerprint() -> str:
    parts = []
    for name, path in sorted(model_files().items()):
        try:
            parts.append('%s:%d' % (name, int(Path(path).stat().st_mtime)))
        except OSError:
            parts.append('%s:0' % name)
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()[:16]


def cache_key(text: str, *, style: str = 'cute', speed: float = 1.0, seed: int = 42) -> str:
    raw = '|'.join([str(style), str(text).strip(), '%.2f' % float(speed), str(int(seed)), _fingerprint()])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]


def synth(text: str, *, style: str = 'cute', speed: float = 1.0, seed: int = 42,
          out_path: str | Path = '', budget_s: float = 300.0, use_cache: bool = True) -> Path | None:
    spoken = ' '.join(str(text or '').split())
    if not spoken:
        return None
    if style not in STYLES:
        style = 'cute'
    key = cache_key(spoken, style=style, speed=speed, seed=seed)
    if use_cache:
        cached = CACHE_DIR / ('cv3_%s.wav' % key)
        if cached.exists() and cached.stat().st_size > 1000:
            return cached
    if not available():
        log.warning('cv3_synth_skipped', reason='not_available', missing=','.join(missing_files()))
        return None
    if not ping(timeout=1.0) and not start_server():
        return None
    target = Path(out_path) if out_path else CACHE_DIR / ('cv3_%s.wav' % key)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    reply = _request({'op': 'say', 'text': spoken, 'style': style, 'out': wsl_path(target),
                      'seed': int(seed), 'speed': float(speed)}, timeout=max(30.0, budget_s))
    if not reply or not reply.get('ok'):
        _fail('server_error', json.dumps(reply, ensure_ascii=False)[:200] if reply else 'no_reply')
        return None
    if not target.exists() or target.stat().st_size < 1000:
        _fail('synth_no_file', str(target))
        return None
    _recovered()
    log.info('cv3_synth_ok', path=target.name, seconds=reply.get('seconds'),
             runtime_s=reply.get('runtime_s'), style=style)
    return target
