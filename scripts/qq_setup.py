"""NapCat（OneBot v11）接入助手：探测 + 写 .env + 验证。

背景
====
NapCat 的安装也自动化了：没有现成安装时，默认从官方 GitHub Release 下载并解压
（``--no-download`` 可关）。唯一必须人工的是手机 QQ 扫码登录（可能还要过设备验证）。
其余步骤本脚本全部自动化：

    1. 找 NapCat 的 OneBot 配置文件（--napcat-dir 或常见目录）
    2. 解析监听端口 / token（兼容几种配置结构）
    3. 调 OneBot HTTP API get_login_info 验证登录，顺带拿到机器人 QQ 号
    4. 把 PLATFORMS_QQ_ACCOUNT / QQ_WS_URL / QQ_HTTP_URL / QQ_ACCESS_TOKEN /
       QQ_SEND_ENABLED 写进 .env（自动备份、原地改、不留重复键）
    5. 可选：对 WS 端口做一次握手验证；提示重启鲸鲸并用体检脚本复核

用法::

    python scripts\\qq_setup.py                      # 自动找配置并探测
    python scripts\\qq_setup.py --napcat-dir D:\\NapCat
    python scripts\\qq_setup.py --http-url http://127.0.0.1:3000 --token xxx
    python scripts\\qq_setup.py --owner 123456 --dry-run   # 只看将写入什么

双击根目录的「QQ接入.bat」等价于交互式运行本脚本。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import webbrowser
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import setup_wizard  # noqa: E402  （复用 .env 写入/备份逻辑）

DEFAULT_HTTP_PORTS = (3000, 3001, 3002, 5700)
DEFAULT_WS_PORTS = (3001, 3002, 6700)
ONE_LINE = "=" * 60


# ---------------------------------------------------------------------------
# 配置解析
# ---------------------------------------------------------------------------


def find_config_files(napcat_dir: Path | None) -> list[Path]:
    """找 NapCat 的 onebot11 配置；napcat_dir 为空时只搜常见目录。"""
    candidates: list[Path] = []
    if napcat_dir:
        candidates.append(napcat_dir)
    candidates.append(ROOT / "napcat")
    home = Path.home()
    for name in ("NapCat", "NapCatQQ", "NapCat.Shell", "NapCat-Framework"):
        candidates.append(home / name)
    candidates.extend([Path("C:/NapCat"), Path("D:/NapCat")])
    found: list[Path] = []
    for base in candidates:
        if not base or not base.exists():
            continue
        for pattern in ("config/onebot11*.json", "onebot11*.json", "**/onebot11_*.json"):
            found.extend(sorted(base.glob(pattern)))
        if found:
            break
    # 去重保序
    seen: set[str] = set()
    unique: list[Path] = []
    for path in found:
        key = str(path.resolve()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _iter_endpoints(container: dict, keys: tuple[str, ...]) -> list[dict]:
    items: list[dict] = []
    for key in keys:
        value = container.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            items.append(value)
    return items


def parse_onebot_config(data: Any) -> dict:
    """从 NapCat/OneBot 配置里抽出 HTTP/WS 监听信息。

    兼容两种常见结构：顶层直接放 httpServers/websocketServers，
    或包在 ``network`` 里；找不到就返回空字段（由端口探测兜底）。
    """
    result: dict[str, Any] = {
        "http": None,
        "ws": None,
        "ws_reverse_only": False,
        "raw_keys": [],
    }
    if not isinstance(data, dict):
        return result
    network = data.get("network") if isinstance(data.get("network"), dict) else data
    result["raw_keys"] = sorted(str(k) for k in network.keys())
    http_items = _iter_endpoints(network, ("httpServers", "httpServer", "http"))
    ws_items = _iter_endpoints(network, ("websocketServers", "wsServers", "websocketServer"))
    ws_clients = _iter_endpoints(network, ("websocketClients", "wsClients"))

    for item in http_items:
        if item.get("enable") is False:
            continue
        port = _as_int(item.get("port"))
        if not port:
            continue
        result["http"] = {
            "host": _local_host(item.get("host")),
            "port": port,
            "token": str(item.get("token") or item.get("accessToken") or ""),
        }
        break
    for item in ws_items:
        if item.get("enable") is False:
            continue
        port = _as_int(item.get("port"))
        if not port:
            continue
        result["ws"] = {
            "host": _local_host(item.get("host")),
            "port": port,
            "token": str(item.get("token") or item.get("accessToken") or ""),
        }
        break
    if result["ws"] is None and ws_clients:
        # 只配了「反向 WS 客户端」：那是 NapCat 连我们，本方案用「正向 WS 服务端」
        result["ws_reverse_only"] = any(item.get("enable") is not False for item in ws_clients)
    return result


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _local_host(host: Any) -> str:
    text = str(host or "").strip()
    if not text or text in ("0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return text


# ---------------------------------------------------------------------------
# OneBot HTTP 探测
# ---------------------------------------------------------------------------


_UNREACHABLE = (ConnectionRefusedError, TimeoutError, socket.timeout, socket.gaierror)


def _transport(exc: BaseException) -> dict:
    """传输层没拿到 HTTP 响应，但要分清两件不同的事：

    - 「根本没连上」（拒绝/域名解析失败/连接超时）→ {"unreachable": ...}，就等于「这个端口上没有 OneBot」；
    - 「连上了但话被打断」（RST/对端半路关掉/响应读到一半）→ {"transport": ...}，必须让调用方看见。
    """
    reason = getattr(exc, "reason", exc)  # urllib 把底层异常塞在 URLError.reason 里
    text = str(reason).strip() or reason.__class__.__name__
    if isinstance(reason, _UNREACHABLE):
        return {"unreachable": text[:200]}
    return {"transport": text[:200]}


def http_api_call(
    base_url: str,
    action: str,
    *,
    token: str = "",
    timeout: float = 3.0,
) -> tuple[int, Any]:
    """调一次 OneBot HTTP API；返回 (状态码, 解析后的 JSON 或错误文本)。"""
    url = f"{base_url.rstrip('/')}/{action}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = b"{}"
    for attempt in ("header", "query"):
        target = url if attempt == "header" else (
            f"{url}?access_token={token}" if token else url
        )
        request = urllib.request.Request(target, data=payload, headers=headers, method="POST")
        if attempt == "query":
            request = urllib.request.Request(target, data=payload,
                                             headers={"Content-Type": "application/json"},
                                             method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    return resp.status, json.loads(body)
                except ValueError:
                    return resp.status, body[:200]
        except urllib.error.HTTPError as exc:
            if attempt == "header" and exc.code in (401, 403):
                continue  # 可能是 token 放在 query 里的实现
            return exc.code, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 - 连接拒绝/超时都算不可用
            if attempt == "header" and token:
                continue
            return 0, _transport(exc)
    return 0, _transport(RuntimeError("两次尝试都没拿到 HTTP 响应"))


def probe_login(base_url: str, *, token: str = "", timeout: float = 3.0) -> dict | None:
    """验证 OneBot 可达且已登录；成功返回 {user_id, nickname, http_url}。"""
    status, payload = http_api_call(base_url, "get_login_info", token=token, timeout=timeout)
    if isinstance(payload, dict) and "unreachable" in payload:
        return None  # 连都没连上：这个端口上没有 OneBot
    if isinstance(payload, dict) and "transport" in payload:
        return {
            "error": "连上了但对话被打断：%s" % payload["transport"],
            "http_url": base_url.rstrip("/"),
            "transport": True,
        }
    if status == 200 and isinstance(payload, dict) and str(payload.get("status")) == "ok":
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            "http_url": base_url.rstrip("/"),
            "user_id": str(data.get("user_id") or ""),
            "nickname": str(data.get("nickname") or ""),
            "token": token,
        }
    if status in (401, 403):
        return {"error": "token 不对（HTTP 401/403）", "http_url": base_url}
    if status == 200 and isinstance(payload, dict):
        return {"error": f"OneBot 返回异常: {str(payload)[:120]}", "http_url": base_url}
    return None


def probe_http_ports(ports: tuple[int, ...], *, token: str = "") -> dict | None:
    for port in ports:
        hit = probe_login(f"http://127.0.0.1:{port}", token=token)
        if isinstance(hit, dict) and hit.get("transport"):
            return hit  # 连上了但话被打断：端口上有东西，不许当成没有
        if hit and "error" not in hit:
            return hit
        if hit and "token 不对" in str(hit.get("error", "")):
            return hit  # 端口是 OneBot，但 token 不对——值得立刻告诉用户
    return None


def probe_ws(ws_url: str, *, timeout: float = 3.0) -> str:
    """正向 WS 握手验证；返回 ok / 错误说明（websockets 不可用时跳过）。"""
    try:
        import asyncio

        import websockets
    except Exception:  # noqa: BLE001
        return "skipped (未安装 websockets)"

    async def _run() -> str:
        try:
            async with websockets.connect(ws_url, open_timeout=timeout) as ws:
                await ws.ping()
            return "ok"
        except Exception as exc:  # noqa: BLE001
            return f"failed: {exc}"[:200]

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# NapCat 安装（官方 GitHub Release）
# ---------------------------------------------------------------------------

DEFAULT_GITHUB_API = "https://api.github.com"
DEFAULT_NAPCAT_REPO = "NapNeko/NapCatQQ"
# GitHub 直连在国内常超时/被限速：默认走「镜像候选链」——
# 先试清华 TUNA（github-release 只镜像精选项目，没收录就 404 快速跳过）
# → 常见 GitHub 代理前缀 → 官方直链；逐条尝试并打印哪条成功。
DEFAULT_GITHUB_PROXIES = (
    "https://gh-proxy.com/",
    "https://mirror.ghproxy.com/",
    "https://ghfast.top/",
)
TUNA_GITHUB_RELEASE = "https://mirrors.tuna.tsinghua.edu.cn/github-release"
_USER_AGENT = "yinyue-qq-setup/0.1"


def github_latest_release(
    *,
    api_url: str = DEFAULT_GITHUB_API,
    repo: str = DEFAULT_NAPCAT_REPO,
    timeout: float = 15.0,
) -> dict:
    url = f"{api_url.rstrip('/')}/repos/{repo}/releases/latest"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _is_github_url(url: str) -> bool:
    low = str(url or "").lower()
    return "github.com/" in low or "githubusercontent.com/" in low


def latest_release_with_mirrors(
    *,
    repo: str = DEFAULT_NAPCAT_REPO,
    api_url: str = "",
    proxies: tuple[str, ...] = DEFAULT_GITHUB_PROXIES,
    timeout: float = 15.0,
    log=print,
) -> tuple[dict | None, str, str]:
    """取 latest release：显式 api_url 直接用；否则按镜像候选链试。

    返回 ``(release, 实际可用的 API base, 说明)``；全失败时 release=None、
    说明里带每一条的错误，方便人直接看出是哪一层不通。
    """
    if api_url:
        candidates = [(api_url, "指定 api_url")]
    else:
        candidates = [(f"{prefix}https://api.github.com", f"镜像 {prefix}")
                      for prefix in proxies]
        candidates.append((DEFAULT_GITHUB_API, "官方 GitHub"))
    errors: list[str] = []
    for base, note in candidates:
        try:
            release = github_latest_release(api_url=base, repo=repo, timeout=timeout)
            return release, base, note
        except Exception as exc:  # noqa: BLE001 - 逐个候选试，失败换下一个
            errors.append(f"{note}: {str(exc)[:70]}")
            log(f"  [!] {note} 取 release 失败：{str(exc)[:70]}")
    return None, "", "；".join(errors)[:300]


def download_with_mirrors(
    url: str,
    dest: Path,
    *,
    proxies: tuple[str, ...] = DEFAULT_GITHUB_PROXIES,
    tuna_url: str = "",
    timeout: float = 60.0,
    chunk: int = 256 * 1024,
    progress=None,
    log=print,
) -> tuple[Path | None, str]:
    """下载 release 资源：非 GitHub 直链直接下；GitHub 走「清华→代理→官方」候选链。"""
    if not _is_github_url(url):
        try:
            return download_file(url, dest, timeout=timeout, chunk=chunk,
                                 progress=progress), "直链"
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)[:200]

    candidates: list[tuple[str, str]] = []
    if tuna_url:
        candidates.append((tuna_url, "清华 TUNA"))
    for prefix in proxies:
        candidates.append((f"{prefix}{url}", f"镜像 {prefix}"))
    candidates.append((url, "官方直链"))
    errors: list[str] = []
    for candidate_url, note in candidates:
        try:
            path = download_file(candidate_url, dest, timeout=timeout, chunk=chunk,
                                 progress=progress)
            return path, note
        except Exception as exc:  # noqa: BLE001 - 逐个候选试
            errors.append(f"{note}: {str(exc)[:70]}")
            log(f"  [!] {note} 下载失败：{str(exc)[:70]}")
    return None, "；".join(errors)[:300]


def score_asset(name: str) -> int:
    """给 release 资源打分：Windows Shell 优先；Linux/mac/安装器一律排除。"""
    low = str(name or "").lower()
    if not low.endswith(".zip"):
        return -1
    score = 0
    if "win" in low or "windows" in low:
        score += 4
    if "shell" in low:
        score += 3
    if "napcat" in low:
        score += 1
    if "framework" in low:
        score -= 2
    for bad in ("linux", "mac", "darwin", "arm", "android", "installer", "lite"):
        if bad in low:
            return -1
    return score


def pick_asset(assets: Any, *, prefer: str = "") -> dict | None:
    if not isinstance(assets, list):
        return None
    candidates = [
        item for item in assets
        if isinstance(item, dict) and str(item.get("name", "")).lower().endswith(".zip")
    ]
    if prefer:
        for item in candidates:
            if prefer.lower() in str(item.get("name", "")).lower():
                return item
    scored = [(score_asset(str(item.get("name", ""))), item) for item in candidates]
    scored = [(score, item) for score, item in scored if score > 0]
    if not scored:
        return None
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("name", ""))))
    return scored[0][1]


def download_file(
    url: str,
    dest: Path,
    *,
    timeout: float = 60.0,
    chunk: int = 256 * 1024,
    progress=None,
) -> Path:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(request, timeout=timeout) as resp, open(temp, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        next_report = 0
        while True:
            block = resp.read(chunk)
            if not block:
                break
            fh.write(block)
            got += len(block)
            if progress and total and got >= next_report:
                progress(int(got * 100 / total), got, total)
                next_report = got + max(total // 20, chunk)  # 每 5% 报一次，方便看清是慢还是卡
    temp.replace(dest)
    return dest


def unzip_archive(archive: Path, dest: Path) -> int:
    """解压 zip 到 dest（带 zip-slip 防护），返回解出的文件数。"""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    count = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(root)):
                raise RuntimeError(f"压缩包路径异常（zip-slip）: {info.filename}")
            zf.extract(info, dest)
            if not info.is_dir():
                count += 1
    return count


def looks_like_napcat(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    for name in ("NapCatWinBootMain.exe", "napcat.bat", "NapCatWinBootHook.dll", "config"):
        if (path / name).exists():
            return True
    return False


def find_napcat_install(explicit: Path | None, *, search_common: bool = True) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    if search_common:
        candidates.extend([
            ROOT / "napcat",
            Path.home() / "NapCat",
            Path.home() / "NapCatQQ",
            Path("C:/NapCat"),
            Path("D:/NapCat"),
        ])
    for candidate in candidates:
        if not candidate or not candidate.exists():
            continue
        if looks_like_napcat(candidate):
            return candidate
        # 压缩包常带一层顶层目录（NapCat/…），往里看一层
        nested = _resolve_install_root(candidate)
        if nested is not None:
            return nested
    return None


def _resolve_install_root(dest: Path) -> Path | None:
    if looks_like_napcat(dest):
        return dest
    try:
        children = sorted(dest.iterdir())
    except OSError:
        return None
    for child in children:
        if child.is_dir() and looks_like_napcat(child):
            return child
    return None


def napcat_running(
    napcat_dir: Path | None = None,
    *,
    ports: tuple[int, ...] | None = None,
    timeout: float = 0.25,
) -> bool:
    """粗判 NapCat 是否已经在跑：WebUI(6099) 或 OneBot HTTP 常见端口的监听探测。

    不依赖 tasklist（受限会话常被拒）；探测失败一律当"没在跑"——宁可多拉起一次，
    NapCat 自己会拒绝重复实例。
    """
    check_ports = ports if ports is not None else (6099,) + tuple(DEFAULT_HTTP_PORTS)
    for port in check_ports:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _is_win11() -> bool:
    try:
        return sys.getwindowsversion().build >= 22000
    except Exception:  # noqa: BLE001
        return False


def _enable_napcat_file_log(napcat_dir: Path) -> None:
    """把 napcat.json 的 fileLog 打开（带备份，失败不挡启动），方便启动失败时查日志。"""
    cfg = Path(napcat_dir) / "napcat" / "config" / "napcat.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        if data.get("fileLog") is True:
            return
        backup = cfg.with_name(cfg.name + f".bak_yinyue_{time.strftime('%Y%m%d_%H%M%S')}")
        backup.write_bytes(cfg.read_bytes())
        data["fileLog"] = True
        cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _find_qq_path(candidates: tuple[Path, ...] | None = None) -> Path | None:
    """找 QQ.exe：显式候选 → 卸载注册表 → 常见安装路径。"""
    for cand in candidates or ():
        if Path(cand).exists():
            return Path(cand)
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
        ) as key:
            uninstall = str(winreg.QueryValueEx(key, "UninstallString")[0] or "")
        exe = Path(uninstall.strip().strip('"')).parent / "QQ.exe"
        if exe.exists():
            return exe
    except Exception:  # noqa: BLE001
        pass
    fallbacks = [Path(r"C:\Program Files\Tencent\QQNT\QQ.exe")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        fallbacks.append(Path(local) / "Programs" / "Tencent" / "QQ" / "QQ.exe")
    for cand in fallbacks:
        if cand.exists():
            return cand
    return None


def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def webui_target(napcat_dir: Path | None) -> tuple[str, int]:
    """返回 (带 token 的 NapCat WebUI 地址, 端口)；读不出 webui.json 就按默认 6099。"""
    base = Path(napcat_dir) if napcat_dir else ROOT / "napcat"
    inner = base / "napcat" if (base / "napcat").is_dir() else base
    port, token = 6099, ""
    try:
        cfg = json.loads((inner / "config" / "webui.json").read_text(encoding="utf-8"))
        port = int(cfg.get("port") or 6099)
        token = str(cfg.get("token") or "")
    except Exception:  # noqa: BLE001
        pass
    url = f"http://127.0.0.1:{port}/webui"
    if token:
        url = f"{url}?token={token}"
    return url, port


def open_webui_when_ready(
    url: str,
    port: int,
    *,
    timeout: float = 60.0,
    interval: float = 1.0,
    opener=None,
    port_check=None,
) -> bool:
    """等 WebUI 端口起来后自动打开浏览器（登录扫码用）；超时/失败返回 False。"""
    opener = opener or webbrowser.open
    port_check = port_check or _port_open
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if port_check("127.0.0.1", int(port)):
            try:
                return bool(opener(url))
            except Exception:  # noqa: BLE001
                return False
        time.sleep(interval)
    return False


def open_webui_when_ready_dir(
    napcat_dir: Path | None,
    *,
    timeout: float = 90.0,
    interval: float = 1.0,
    opener=None,
    port_check=None,
) -> bool:
    """等 NapCat WebUI 起来后自动打开；端口就绪后**重读 webui.json**。

    新电脑首次运行时 token 是 NapCat 启动后才写进 webui.json 的——先算好的 URL 会缺 token，
    所以这里等端口起来再读一次。
    """
    opener = opener or webbrowser.open
    port_check = port_check or _port_open
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        _url, port = webui_target(napcat_dir)
        if port_check("127.0.0.1", int(port)):
            url, _port = webui_target(napcat_dir)
            try:
                return bool(opener(url))
            except Exception:  # noqa: BLE001
                return False
        time.sleep(interval)
    return False


def _schedule_webui_open(napcat_dir: Path) -> None:
    threading.Thread(target=open_webui_when_ready_dir, args=(napcat_dir,), daemon=True,
                     name="napcat-webui-open").start()


def start_napcat_detached(
    napcat_dir: Path | None,
    *,
    qq_path: Path | None = None,
    open_webui: bool = True,
) -> tuple[bool, str]:
    """无窗口常驻启动 NapCat（注入式）：不弹控制台，也就没有"关窗=停服"。

    复刻 `launcher-user.bat`：写 loadNapCat.js → 设 5 个 NAPCAT_* 环境变量 →
    运行 `NapCatWinBootMain.exe <QQ.exe> <NapCatWinBootHook.dll>`（CREATE_NO_WINDOW）。
    """
    if napcat_dir is None:
        return False, "没拿到 NapCat 目录"
    base = Path(napcat_dir)
    inner = base / "napcat" if (base / "napcat").is_dir() else base
    main = inner / "NapCatWinBootMain.exe"
    hook = inner / "NapCatWinBootHook.dll"
    mjs = inner / "napcat.mjs"
    patch = inner / "qqnt.json"
    missing = [p.name for p in (main, hook, mjs, patch) if not p.exists()]
    if missing:
        return False, f"这个包不是 Shell 注入版（缺 {', '.join(missing)}）"
    qq = Path(qq_path) if qq_path else _find_qq_path()
    if qq is None or not qq.exists():
        return False, "没找到 QQ.exe（注册表与常见路径都没有）：先装 QQ，或用 launcher 控制台方式"
    _enable_napcat_file_log(base)
    load = inner / "loadNapCat.js"
    load.write_text(
        f'(async () => {{await import("file:///{str(mjs).replace(chr(92), chr(47))}")}})()',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.update({
        "NAPCAT_PATCH_PACKAGE": str(patch),
        "NAPCAT_LOAD_PATH": str(load),
        "NAPCAT_INJECT_PATH": str(hook),
        "NAPCAT_LAUNCHER_PATH": str(main),
        "NAPCAT_MAIN_PATH": str(mjs).replace(chr(92), "/"),
    })
    try:
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        subprocess.Popen([str(main), str(qq), str(hook)], cwd=str(inner), env=env,
                         creationflags=flags)
    except Exception as exc:  # noqa: BLE001
        return False, f"启动失败: {exc}"
    url, _port = webui_target(base)
    if open_webui:
        _schedule_webui_open(base)
    return True, (f"已无窗口拉起 NapCat（QQ: {qq}）——没有控制台窗口，不存在'关窗=停服'；"
                  f"登录入口：{url}")


def stop_napcat(*, elevate: bool = False) -> tuple[bool, str]:
    """停 NapCat = 先杀常驻守护 NapCatWinBootMain，再杀 QQ.exe；用端口校验结果。

    判定不依赖 taskkill 的输出文字（中英文/代码页差异会骗人）：
    杀完轮询 WebUI 端口——关了才算停成；没关就需要管理员（elevate=True 弹 UAC）。
    """
    flags = 0x08000000 if os.name == "nt" else 0

    def _kill_all() -> None:
        for image in ("NapCatWinBootMain.exe", "QQ.exe"):
            try:
                subprocess.run(["taskkill", "/F", "/IM", image], capture_output=True,
                               timeout=15, creationflags=flags)
            except Exception:  # noqa: BLE001
                pass

    def _stopped(timeout: float = 8.0) -> bool:
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            try:
                _url, port = webui_target(ROOT / "napcat")
            except Exception:  # noqa: BLE001
                port = 6099
            if not _port_open("127.0.0.1", int(port)):
                return True
            time.sleep(0.5)
        return False

    _kill_all()
    if _stopped():
        return True, "已停止 NapCat（守护 + QQ.exe）"
    if elevate and os.name == "nt":
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 ("Start-Process cmd -ArgumentList '/c',"
                  "'taskkill /F /IM NapCatWinBootMain.exe & taskkill /F /IM QQ.exe' "
                  "-Verb RunAs -Wait")],
                capture_output=True, timeout=120, creationflags=flags)
        except Exception as exc:  # noqa: BLE001
            return False, f"提权停止失败: {exc}"
        if _stopped(timeout=12.0):
            return True, "已停止 NapCat（管理员：守护 + QQ.exe）"
        if proc.returncode != 0:
            return False, "提权未完成（UAC 被取消？）：请重试并在弹窗点「是」"
        return False, "已提权停止，但 NapCat 仍在运行（可能又被拉起）：请检查任务管理器"
    return False, ("停止失败：当前权限杀不掉（QQ/NapCat 以管理员运行），"
                   "请加 --elevate 重新执行（会弹 UAC），或从启动器点「停止 NapCat」。")


def start_napcat(napcat_dir: Path | None, *, detached: bool = True,
                 open_webui: bool = True) -> tuple[bool, str]:
    """启动 NapCat：默认无窗口常驻（注入式）；注入版缺件时退回 launcher 控制台方式。

    2026-09-25 实测：Shell 包的正牌入口是 ``napcat/launcher-user.bat``（自动找 QQ 路径并注入）；
    ``napcat.bat``（node 直跑）在部分机器 fork worker 会 `spawn EPERM`。
    """
    if detached:
        ok, note = start_napcat_detached(napcat_dir, open_webui=open_webui)
        if ok:
            return True, note
    if napcat_dir is None:
        return False, "没拿到 NapCat 目录"
    base = Path(napcat_dir)
    folders = [base / "napcat", base]
    if _is_win11():
        names = ["launcher-user.bat", "launcher.bat"]
    else:
        names = ["launcher-win10-user.bat", "launcher-win10.bat",
                 "launcher-user.bat", "launcher.bat"]
    names = names + ["napcat.bat"]
    for folder in folders:
        for name in names:
            bat = folder / name
            if not bat.exists():
                continue
            _enable_napcat_file_log(base)
            try:
                flags = 0x00000010 if os.name == "nt" else 0  # CREATE_NEW_CONSOLE
                subprocess.Popen([str(bat)], cwd=str(folder), creationflags=flags)
            except Exception as exc:  # noqa: BLE001
                return False, f"启动失败: {exc}"
            url, _port = webui_target(base)
            if open_webui:
                _schedule_webui_open(base)
            return True, (f"已启动 NapCat（{name}）：{folder}（窗口别关：关掉=服务停）；"
                          f"登录入口：{url}")
    return False, f"没找到启动脚本（找过 {folders[0]} 与 {folders[1]}）"


def ensure_onebot_network(
    napcat_dir: Path | None,
    *,
    token: str = "",
    host: str = "127.0.0.1",
    http_port: int = 3000,
    ws_port: int = 3001,
) -> tuple[bool, str]:
    """把 OneBot11 配置的「HTTP + 正向 WS」打开：合并式（只补不删）+ 备份 + 改后校验。

    返回 (配置是否可用, 说明)。配置文件不存在时返回 False——先启动 NapCat 登录一次，
    它会自动生成 ``config/onebot11_<QQ>.json``，之后本函数才能补写。
    """
    configs = find_config_files(napcat_dir)
    if not configs:
        return False, "没找到 OneBot 配置：先启动 NapCat 并登录一次（会自动生成 onebot11_<QQ>.json）"
    path = configs[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"配置解析失败（{path.name}）: {str(exc)[:120]}"
    parsed = parse_onebot_config(data)
    if parsed.get("http") and parsed.get("ws"):
        return True, f"HTTP/WS 已开启（{parsed['http']['port']}/{parsed['ws']['port']}）"
    network = data.get("network") if isinstance(data.get("network"), dict) else data
    if not isinstance(network, dict):
        return False, "配置结构不认识（既没有 network 也不是对象），请到 WebUI 手动开 OneBot"

    changed = False
    if not parsed.get("http"):
        servers = network.setdefault("httpServers", [])
        if isinstance(servers, list):
            servers.append({
                "enable": True, "name": "yinyue-http", "host": host,
                "port": int(http_port), "enableCors": True, "enableWebsocket": False,
                "messagePostFormat": "array", "token": token, "debug": False,
                "reportSelfMessage": False,
            })
            changed = True
    if not parsed.get("ws"):
        servers = network.setdefault("websocketServers", [])
        if isinstance(servers, list):
            servers.append({
                "enable": True, "name": "yinyue-ws", "host": host,
                "port": int(ws_port), "messagePostFormat": "array",
                "reportSelfMessage": False, "token": token, "debug": False,
                "heartInterval": 30000,
            })
            changed = True
    if not changed:
        return False, "配置里没有可写入的 httpServers/websocketServers，请到 WebUI 手动开"

    backup = path.with_name(path.name + f".bak_yinyue_{time.strftime('%Y%m%d_%H%M%S')}")
    backup.write_bytes(path.read_bytes())
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    again = parse_onebot_config(json.loads(path.read_text(encoding="utf-8")))
    if not (again.get("http") and again.get("ws")):
        path.write_bytes(backup.read_bytes())
        return False, "写入后校验没通过，已回滚（请到 WebUI 手动开 OneBot）"
    return True, (f"已自动写入并校验：HTTP {http_port} + 正向 WS {ws_port}"
                  f"（备份 {backup.name}）；重启 NapCat 后生效")


def current_login_account(napcat_dir: Path | None) -> str:
    """从 config/napcat_<QQ>.json 的最近修改时间推断当前登录的 QQ 号。"""
    base = Path(napcat_dir) if napcat_dir else (ROOT / "napcat")
    inner = base / "napcat" if (base / "napcat").is_dir() else base
    cfg = inner / "config"
    if not cfg.is_dir():
        return ""
    rows: list[tuple[float, str]] = []
    for item in cfg.glob("napcat_*.json"):
        digits = item.stem.split("_", 1)[1] if "_" in item.stem else ""
        if digits.isdigit():
            rows.append((item.stat().st_mtime, digits))
    if not rows:
        return ""
    rows.sort(reverse=True)
    return rows[0][1]


def ensure_current_onebot(napcat_dir: Path | None, *, http_port: int = 3000,
                          ws_port: int = 3001) -> tuple[str, str]:
    """确保「当前登录 QQ」的 OneBot 通道开着（缺则自动补，需重启才生效）。

    返回 (status, 说明)：status ∈ ok / restart / notfound / error
    """
    if _port_open("127.0.0.1", int(http_port)):
        return "ok", f"OneBot 通道已就绪（HTTP {http_port} 已在服务）"
    qq = current_login_account(napcat_dir)
    if not qq:
        return "notfound", "找不到登录记录：先启动 NapCat 并登录一次 QQ"
    base = Path(napcat_dir) if napcat_dir else (ROOT / "napcat")
    inner = base / "napcat" if (base / "napcat").is_dir() else base
    cfg_path = inner / "config" / f"onebot11_{qq}.json"
    if not cfg_path.exists():
        return "notfound", f"当前登录 {qq} 还没有 OneBot 配置：完成一次登录后重试"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        parsed = parse_onebot_config(data)
    except Exception as exc:  # noqa: BLE001
        return "error", f"读取 onebot 配置失败: {str(exc)[:120]}"
    if parsed.get("http") and parsed.get("ws"):
        return "ok", f"OneBot 通道已就绪（{qq}：HTTP/WS 已开）"
    network = data.get("network") if isinstance(data.get("network"), dict) else data
    if not isinstance(network, dict):
        return "error", "配置结构不认识（既没有 network 也不是对象）"
    changed = False
    if not parsed.get("http"):
        servers = network.setdefault("httpServers", [])
        if isinstance(servers, list):
            servers.append({
                "enable": True, "name": "yinyue-http", "host": "127.0.0.1",
                "port": int(http_port), "enableCors": True, "enableWebsocket": False,
                "messagePostFormat": "array", "token": "", "debug": False,
                "reportSelfMessage": False,
            })
            changed = True
    if not parsed.get("ws"):
        servers = network.setdefault("websocketServers", [])
        if isinstance(servers, list):
            servers.append({
                "enable": True, "name": "yinyue-ws", "host": "127.0.0.1",
                "port": int(ws_port), "messagePostFormat": "array",
                "reportSelfMessage": False, "token": "", "debug": False,
                "heartInterval": 30000, "enableForcePushEvent": True,
            })
            changed = True
    if not changed:
        return "error", "配置里没有可写入的 httpServers/websocketServers"
    backup = cfg_path.with_name(cfg_path.name + f".bak_yinyue_{time.strftime('%Y%m%d_%H%M%S')}")
    backup.write_bytes(cfg_path.read_bytes())
    cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    again = parse_onebot_config(json.loads(cfg_path.read_text(encoding="utf-8")))
    if not (again.get("http") and again.get("ws")):
        cfg_path.write_bytes(backup.read_bytes())
        return "error", "写入后校验没通过，已回滚"
    return "restart", (f"已自动补写 {qq} 的 OneBot 通道（HTTP {http_port} + WS {ws_port}，"
                       f"备份 {backup.name}）——重启 NapCat 后生效")


def ensure_napcat_install(
    install_dir: Path | None,
    *,
    auto_download: bool = True,
    api_url: str = "",
    repo: str = DEFAULT_NAPCAT_REPO,
    asset_hint: str = "",
    proxy: str = "",
    timeout: float = 60.0,
    search_common: bool = True,
) -> tuple[Path | None, str]:
    """检测已有安装；没有且允许时下载解压（默认走镜像候选链）。返回 (目录, 状态说明)。"""
    proxies = (proxy,) if proxy else DEFAULT_GITHUB_PROXIES
    existing = find_napcat_install(install_dir, search_common=search_common)
    if existing is not None:
        return existing, "already"
    if not auto_download:
        return None, "no-download"
    release, _api_base, note = latest_release_with_mirrors(
        repo=repo, api_url=api_url, proxies=proxies, timeout=timeout,
    )
    if release is None:
        return None, f"github-failed: {note}"[:300]
    if note:
        print(f"  源: {note}")
    tag = str(release.get("tag_name") or "")
    assets = release.get("assets") or []
    asset = pick_asset(assets, prefer=asset_hint)
    if asset is None:
        names = ", ".join(str(item.get("name")) for item in assets[:8])
        return None, f"no-asset (tag={tag}; 候选: {names})"
    url = str(asset.get("browser_download_url") or "")
    name = str(asset.get("name") or "napcat.zip")
    size = _as_int(asset.get("size"))
    target = Path(install_dir) if install_dir else (ROOT / "napcat")
    target.mkdir(parents=True, exist_ok=True)
    archive = target / ".download" / name
    print(f"  资源: {repo} {tag} / {name}" + (f"（{size // 1024 // 1024}MB）" if size else ""))
    # 显式给了 --github-proxy 就只走「自定义源 → 官方兜底」，不再先试清华
    tuna_url = (f"{TUNA_GITHUB_RELEASE}/{repo}/{tag}/{name}"
                if tag and name and not proxy else "")
    path, note = download_with_mirrors(
        url, archive, proxies=proxies, tuna_url=tuna_url, timeout=timeout,
        progress=lambda pct, got, total: print(
            f"  下载 {pct}%（{got // 1048576}MB / {total // 1048576}MB）"
        ),
    )
    if path is None:
        return None, f"download-failed: {note}"[:300]
    print("  解压中…")
    try:
        files = unzip_archive(archive, target)
    except Exception as exc:  # noqa: BLE001
        return None, f"unzip-failed: {exc}"[:200]
    print(f"  已解压 {files} 个文件 → {target}")
    root = _resolve_install_root(target)
    if root is None:
        return None, f"downloaded-but-not-napcat (解压 {files} 个文件，未识别出 NapCat 结构)"
    return root, f"downloaded:{tag}:{name}"


# ---------------------------------------------------------------------------
# .env 写入
# ---------------------------------------------------------------------------


def build_env_values(
    *,
    qq_account: str,
    http_url: str,
    ws_url: str,
    token: str,
    send_enabled: bool,
    owner: str = "",
) -> dict[str, str]:
    values = {
        "QQ_HTTP_URL": http_url,
        "QQ_WS_URL": ws_url,
        "QQ_ACCESS_TOKEN": token,
        "QQ_SEND_ENABLED": "true" if send_enabled else "false",
    }
    if qq_account:
        values["PLATFORMS_QQ_ACCOUNT"] = qq_account
    if owner:
        values["PLATFORMS_QQ_OWNER_ACCOUNTS"] = owner
    return values


def write_env(root: Path, values: dict[str, str], *, dry_run: bool = False) -> dict[str, str]:
    env_path = root / ".env"
    if not env_path.exists():
        return {}
    if dry_run:
        return {key: "would-update" for key in values}
    setup_wizard.backup_file(env_path, "qq_setup")
    return setup_wizard.set_env_values(env_path, values)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NapCat（OneBot v11）接入助手")
    parser.add_argument("--napcat-dir", default="", help="NapCat 安装目录（找 onebot11 配置用）")
    parser.add_argument("--http-url", default="", help="直接指定 OneBot HTTP 地址")
    parser.add_argument("--ws-url", default="", help="直接指定 OneBot 正向 WS 地址")
    parser.add_argument("--token", default="", help="OneBot access token")
    parser.add_argument("--owner", default="", help="主人 QQ 号（写 PLATFORMS_QQ_OWNER_ACCOUNTS）")
    parser.add_argument("--no-send", action="store_true", help="不打开发送（默认打开）")
    parser.add_argument("--dry-run", action="store_true", help="只探测，不写 .env")
    parser.add_argument("--no-download", action="store_true",
                        help="不自动下载 NapCat（默认联网下载官方 release）")
    parser.add_argument("--no-start", action="store_true",
                        help="不自动启动 NapCat（默认会弹控制台方便扫码）")
    parser.add_argument("--start", action="store_true",
                        help="强制启动 NapCat（已装但没在跑时会自动拉起，无需加这个）")
    parser.add_argument("--install-dir", default="", help="NapCat 安装目录（默认 ./napcat）")
    parser.add_argument("--asset", default="", help="release 资源名关键词（如 NapCat.Shell）")
    parser.add_argument("--github-api", default="",
                        help="GitHub API 地址（留空=内置镜像候选链；测试/自建镜像用）")
    parser.add_argument("--github-proxy", default=os.environ.get("QQ_GITHUB_PROXY", ""),
                        help="GitHub 下载代理前缀（如 https://gh-proxy.com/；留空=内置候选链）")
    parser.add_argument("--project-root", default="", help="项目目录（默认取本脚本上级）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.project_root).resolve() if args.project_root else ROOT
    napcat_dir = Path(args.napcat_dir).resolve() if args.napcat_dir else None

    print(ONE_LINE)
    print("  鲸鲸 · NapCat（OneBot v11）接入助手")
    print(ONE_LINE)

    # 1) 准备 NapCat（检测 / 默认联网下载官方 release）
    print("\n[1/5] 检查 NapCat")
    install_dir = Path(args.install_dir).resolve() if args.install_dir else None
    resolved_napcat, napcat_status = ensure_napcat_install(
        install_dir,
        auto_download=not args.no_download,
        api_url=args.github_api,
        asset_hint=args.asset,
        proxy=args.github_proxy,
    )
    if resolved_napcat is not None:
        napcat_dir = resolved_napcat
        if napcat_status == "already":
            print(f"  已安装: {resolved_napcat}")
        else:
            print(f"  已下载并解压: {resolved_napcat}（{napcat_status}）")
        if not args.no_start:
            running = napcat_running(resolved_napcat)
            if napcat_status.startswith("downloaded") or args.start or not running:
                started, note = start_napcat(resolved_napcat)
                print(f"  {'✓' if started else '[!]'} {note}")
            else:
                print("  NapCat 已经在跑（端口探测），跳过启动")
        print("  提示：还没登录/开服务的话 → 启动 NapCat → 手机 QQ 扫码 → "
              "OneBot 的 HTTP + 正向 WS 没开的话，重跑本助手会自动补好")
    else:
        if napcat_status.startswith("github-failed"):
            print(f"  [!] 联网下载失败: {napcat_status}")
            print("      可手动从官方 GitHub Release 下载 → 解压 → 用 --napcat-dir <目录> 重跑本脚本")
        elif napcat_status == "no-download":
            print("  已按 --no-download 跳过下载（需要时去掉该参数）")
        else:
            print(f"  [!] 没有找到可用安装包: {napcat_status}")

    # 2) 找配置
    print("\n[2/5] 找 NapCat 的 OneBot 配置")
    configs = find_config_files(napcat_dir)
    parsed: dict[str, Any] = {"http": None, "ws": None, "ws_reverse_only": False, "raw_keys": []}
    if configs:
        print(f"  找到 {len(configs)} 个配置，试解析: {configs[0].name}")
        for path in configs:
            try:
                parsed = parse_onebot_config(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                print(f"  [!] {path.name} 解析失败: {exc}")
                continue
            if parsed.get("http") or parsed.get("ws"):
                print(f"  解析成功: {path}")
                break
    else:
        print("  没找到配置文件（没装、装在别处、或还没开 OneBot 服务）")
        print("  提示：装好后在 NapCat WebUI 里开启「OneBot v11」的 HTTP + 正向 WS 服务")
        print("        也可以用 --http-url/--token 直接联调")

    # 2) 探测 HTTP
    print("\n[3/5] 验证 OneBot HTTP 是否可用（get_login_info）")
    http_url = args.http_url or ""
    token = args.token
    if not http_url and parsed.get("http"):
        http_url = f"http://{parsed['http']['host']}:{parsed['http']['port']}"
        token = token or parsed["http"].get("token", "")
    login: dict | None = None
    if http_url:
        login = probe_login(http_url, token=token)
        if login and "error" not in login:
            print(f"  连接成功: {http_url}（机器人: {login['nickname']} / {login['user_id']}）")
        elif login and "error" in login:
            print(f"  [!] {http_url}: {login['error']}")
    if not login or "error" in login:
        print("  按常见端口再试一轮:", ", ".join(str(p) for p in DEFAULT_HTTP_PORTS))
        found = probe_http_ports(DEFAULT_HTTP_PORTS, token=token)
        if found and "error" not in found:
            login = found
            print(f"  连接成功: {found['http_url']}（机器人: {found['nickname']} / {found['user_id']}）")
        elif found and "error" in found:
            print(f"  [!] {found['http_url']}: {found['error']}")
        else:
            print("  [!] 没连上任何 OneBot HTTP 端口。请确认 NapCat 已启动、QQ 已登录、OneBot HTTP 开着")

    # 3) WS
    print("\n[4/5] 检查正向 WS")
    ws_url = args.ws_url or ""
    if not ws_url and parsed.get("ws"):
        ws_url = f"ws://{parsed['ws']['host']}:{parsed['ws']['port']}"
        if not token:
            token = parsed["ws"].get("token", "")
    if parsed.get("ws_reverse_only") and not ws_url:
        print("  [!] 你配的是「反向 WS 客户端」（NapCat 连我们）；本方案要「正向 WS 服务端」")
        print("      去 NapCat WebUI 加一个 WebSocket 服务端（记下端口）")
    if ws_url:
        if token and "access_token=" not in ws_url:
            sep = "&" if "?" in ws_url else "?"
            ws_url_with_token = f"{ws_url}{sep}access_token={token}"
        else:
            ws_url_with_token = ws_url
        result = probe_ws(ws_url_with_token)
        print(f"  WS 握手: {result}（{ws_url}）")
    else:
        print("  没拿到 WS 地址；发送能用，但收消息必须有正向 WS")

    # 4) 写 .env
    print("\n[5/5] 写 .env")
    if not login or "error" in login:
        print("  [!] 没验证通过，先不写 .env。")
        print("      最快路径：NapCat WebUI → OneBot v11 → 开 HTTP+WS → 重跑本脚本")
        print("      或显式指定: python scripts\\qq_setup.py --http-url <url> --ws-url <url> --token <token>")
        return 1
    values = build_env_values(
        qq_account=login.get("user_id", ""),
        http_url=login.get("http_url", http_url),
        ws_url=ws_url,
        token=token,
        send_enabled=not args.no_send,
        owner=args.owner,
    )
    actions = write_env(root, values, dry_run=args.dry_run)
    for key, action in actions.items():
        shown = values[key]
        if key == "QQ_ACCESS_TOKEN" and shown:
            shown = shown[:6] + "..." + shown[-4:]
        print(f"  {key} = {shown}（{action}）")
    if args.dry_run:
        print("  （--dry-run：没有真的写）")
    if not values.get("PLATFORMS_QQ_OWNER_ACCOUNTS") and not args.owner:
        print("  [!] 提醒：主人 QQ 号还没配（PLATFORMS_QQ_OWNER_ACCOUNTS），")
        print("      配置方法见 docs\\QQ平台接入方案.md §3 / §11")
    print("\n下一步：重启鲸鲸（先停旧再起）→ 日志看 qq_provider_connected / qq_ws_connected")
    print("体检：双击 体检鲸鲸.bat（或 python scripts\\doctor.py）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
