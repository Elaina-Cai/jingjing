"""NapCat 接入助手（scripts/qq_setup.py）回归测试。

不需要真 NapCat：
- 配置解析用 3 种结构样本；
- HTTP 探测用本地假 OneBot（threading http.server）；
- .env 写入用项目内临时目录。
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import socket
import struct
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import qq_setup  # noqa: E402

TMP = ROOT / ".tmp_test" / "qq_setup_test"
TOKEN = "test-token"


def _fresh(name: str) -> Path:
    d = TMP / name
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_parse_config_network_wrapper() -> None:
    data = {
        "network": {
            "httpServers": [
                {"name": "http", "enable": True, "host": "0.0.0.0", "port": 3000, "token": TOKEN}
            ],
            "websocketServers": [
                {"name": "ws", "enable": True, "host": "0.0.0.0", "port": 3001, "token": TOKEN}
            ],
        }
    }
    parsed = qq_setup.parse_onebot_config(data)
    assert parsed["http"] == {"host": "127.0.0.1", "port": 3000, "token": TOKEN}
    assert parsed["ws"] == {"host": "127.0.0.1", "port": 3001, "token": TOKEN}
    assert parsed["ws_reverse_only"] is False


def test_parse_config_top_level_and_disabled() -> None:
    data = {
        "httpServer": {"enable": False, "port": 4000},
        "httpServers": [{"enable": True, "host": "127.0.0.1", "port": 4001}],
        "websocketClients": [{"enable": True, "url": "ws://127.0.0.1:5000"}],
    }
    parsed = qq_setup.parse_onebot_config(data)
    assert parsed["http"]["port"] == 4001
    assert parsed["ws"] is None
    assert parsed["ws_reverse_only"] is True


def test_parse_config_garbage() -> None:
    parsed = qq_setup.parse_onebot_config({})
    assert parsed["http"] is None and parsed["ws"] is None
    assert qq_setup.parse_onebot_config("nonsense")["http"] is None


def test_find_config_files() -> None:
    d = _fresh("napcat")
    (d / "config").mkdir(parents=True, exist_ok=True)
    target = d / "config" / "onebot11_10001.json"
    target.write_text("{}", encoding="utf-8")
    found = qq_setup.find_config_files(d)
    assert target in found


class _FakeOneBotHandler(BaseHTTPRequestHandler):
    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server 约定
        # 先把请求体读掉再回话：带未读入站数据关闭套接字，Windows 上发的是 RST 而不是 FIN，
        # 客户端于是读到「连接被重置」而不是我们的 401——全量里表现为偶发失败。
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if not self.path.split("?")[0].endswith("/get_login_info"):
            self._reply(404, {"status": "failed", "retcode": 404})
            return
        auth = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        query = self.path.split("access_token=")[1] if "access_token=" in self.path else ""
        if (auth or query) != TOKEN:
            self._reply(401, {"status": "failed", "retcode": 1})
            return
        self._reply(200, {"status": "ok", "retcode": 0,
                          "data": {"user_id": 10001, "nickname": "鲸鲸测试"}})

    def log_message(self, *args) -> None:  # noqa: D102 - 静音
        return


def _with_fake_onebot(callback):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOneBotHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return callback(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def test_probe_login_ok_and_token_error() -> None:
    def scenario(port: int):
        good = qq_setup.probe_login(f"http://127.0.0.1:{port}", token=TOKEN)
        bad = qq_setup.probe_login(f"http://127.0.0.1:{port}", token="wrong")
        found = qq_setup.probe_http_ports((port,), token=TOKEN)
        return port, good, bad, found

    port, good, bad, found = _with_fake_onebot(scenario)
    assert good and good["user_id"] == "10001" and good["nickname"] == "鲸鲸测试"
    assert bad and "token" in bad["error"]
    assert found and found["http_url"] == f"http://127.0.0.1:{port}"


def test_probe_http_ports_miss() -> None:
    assert qq_setup.probe_http_ports((1,), token="") is None


def test_build_env_values() -> None:
    values = qq_setup.build_env_values(
        qq_account="10001",
        http_url="http://127.0.0.1:3000",
        ws_url="ws://127.0.0.1:3001",
        token="tok",
        send_enabled=True,
        owner="20002",
    )
    assert values["PLATFORMS_QQ_ACCOUNT"] == "10001"
    assert values["QQ_HTTP_URL"] == "http://127.0.0.1:3000"
    assert values["QQ_WS_URL"] == "ws://127.0.0.1:3001"
    assert values["QQ_ACCESS_TOKEN"] == "tok"
    assert values["QQ_SEND_ENABLED"] == "true"
    assert values["PLATFORMS_QQ_OWNER_ACCOUNTS"] == "20002"


def test_write_env_updates_and_backs_up() -> None:
    d = _fresh("project")
    env = d / ".env"
    env.write_text("PROVIDER=deepseek\nPLATFORMS_QQ_OWNER_ACCOUNTS=20002\n",
                   encoding="utf-8")
    actions = qq_setup.write_env(d, {
        "PLATFORMS_QQ_ACCOUNT": "10001",
        "QQ_WS_URL": "ws://127.0.0.1:3001",
    })
    text = env.read_text(encoding="utf-8")
    assert actions["PLATFORMS_QQ_ACCOUNT"] == "added"
    assert "PLATFORMS_QQ_ACCOUNT=10001" in text
    assert "QQ_WS_URL=ws://127.0.0.1:3001" in text
    assert "PLATFORMS_QQ_OWNER_ACCOUNTS=20002" in text  # 不覆盖已有主人号
    assert list(d.glob(".env.bak_qq_setup*")), "应留备份"


def test_write_env_dry_run_and_missing() -> None:
    d = _fresh("project_dry")
    env = d / ".env"
    env.write_text("PROVIDER=deepseek\n", encoding="utf-8")
    actions = qq_setup.write_env(d, {"QQ_WS_URL": "ws://x"}, dry_run=True)
    assert actions == {"QQ_WS_URL": "would-update"}
    assert "QQ_WS_URL" not in env.read_text(encoding="utf-8")
    empty = _fresh("project_empty")
    assert qq_setup.write_env(empty, {"A": "1"}) == {}


def _make_napcat_zip(path: Path) -> bytes:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("NapCat/NapCatWinBootMain.exe", "stub")
        zf.writestr("NapCat/config/onebot11_10001.json", "{}")
    return path.read_bytes()


class _FakeReleaseHandler(BaseHTTPRequestHandler):
    zip_bytes = b""

    def _reply(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server 约定
        port = self.server.server_address[1]
        if self.path.startswith("/repos/") and self.path.endswith("/releases/latest"):
            payload = {
                "tag_name": "v-test",
                "assets": [
                    {"name": "NapCat.Framework.zip", "size": len(self.zip_bytes),
                     "browser_download_url": f"http://127.0.0.1:{port}/asset/NapCat.Framework.zip"},
                    {"name": "NapCat.Shell.zip", "size": len(self.zip_bytes),
                     "browser_download_url": f"http://127.0.0.1:{port}/asset/NapCat.Shell.zip"},
                ],
            }
            self._reply(200, json.dumps(payload).encode("utf-8"))
            return
        if self.path == "/asset/NapCat.Shell.zip":
            self._reply(200, type(self).zip_bytes, "application/zip")
            return
        self._reply(404, b"{}")

    def log_message(self, *args) -> None:  # noqa: D102 - 静音
        return


def test_score_and_pick_asset() -> None:
    assets = [
        {"name": "NapCat.Shell.zip"},
        {"name": "NapCat.Shell.Linux.zip"},
        {"name": "NapCat.Framework.zip"},
        {"name": "something.exe"},
    ]
    picked = qq_setup.pick_asset(assets)
    assert picked and picked["name"] == "NapCat.Shell.zip"
    assert qq_setup.pick_asset(assets, prefer="framework")["name"] == "NapCat.Framework.zip"
    assert qq_setup.score_asset("NapCat.Shell.Linux.zip") == -1
    assert qq_setup.pick_asset([]) is None


def test_download_file_and_unzip() -> None:
    d = _fresh("download")
    zip_bytes = _make_napcat_zip(d / "src.zip")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeReleaseHandler)
    _FakeReleaseHandler.zip_bytes = zip_bytes
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        target = d / "downloaded.zip"
        qq_setup.download_file(f"http://127.0.0.1:{port}/asset/NapCat.Shell.zip", target)
        assert target.read_bytes() == zip_bytes
        out = d / "out"
        files = qq_setup.unzip_archive(target, out)
        assert files == 2
        assert (out / "NapCat" / "NapCatWinBootMain.exe").exists()
        assert qq_setup.looks_like_napcat(out / "NapCat")
    finally:
        server.shutdown()
        server.server_close()


def test_unzip_rejects_zip_slip() -> None:
    d = _fresh("zipslip")
    bad = d / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.txt", "x")
    try:
        qq_setup.unzip_archive(bad, d / "out")
    except RuntimeError as exc:
        assert "zip-slip" in str(exc)
    else:  # pragma: no cover - 必须抛
        raise AssertionError("zip-slip 没有被拦截")


def test_ensure_napcat_download_and_no_download() -> None:
    d = _fresh("ensure_dl")
    zip_bytes = _make_napcat_zip(d / "src.zip")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeReleaseHandler)
    _FakeReleaseHandler.zip_bytes = zip_bytes
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        install_dir = d / "napcat"
        resolved, status = qq_setup.ensure_napcat_install(
            install_dir,
            auto_download=True,
            api_url=f"http://127.0.0.1:{port}",
            search_common=False,
        )
        assert resolved is not None and status.startswith("downloaded"), status
        assert (resolved / "NapCatWinBootMain.exe").exists()
        again, status2 = qq_setup.ensure_napcat_install(
            install_dir,
            auto_download=True,
            api_url=f"http://127.0.0.1:{port}",
            search_common=False,
        )
        assert again == resolved and status2 == "already"
    finally:
        server.shutdown()
        server.server_close()

    empty = _fresh("ensure_nodl")
    resolved2, status3 = qq_setup.ensure_napcat_install(
        empty / "napcat", auto_download=False, search_common=False
    )
    assert resolved2 is None and status3 == "no-download"


def test_latest_release_with_mirrors_falls_back() -> None:
    calls: list[str] = []
    original = qq_setup.github_latest_release

    def fake(*, api_url="", repo="", timeout=0.0):
        calls.append(api_url)
        if not api_url.startswith("https://api.github.com"):
            raise OSError("mirror down")
        return {"tag_name": "v9"}

    qq_setup.github_latest_release = fake
    try:
        release, base, note = qq_setup.latest_release_with_mirrors(
            repo="r", timeout=0.1, log=lambda *_: None
        )
    finally:
        qq_setup.github_latest_release = original
    assert release == {"tag_name": "v9"}, release
    assert base == qq_setup.DEFAULT_GITHUB_API and note == "官方 GitHub", (base, note)
    assert len(calls) == len(qq_setup.DEFAULT_GITHUB_PROXIES) + 1, calls


def test_latest_release_explicit_api_bypasses_mirrors() -> None:
    calls: list[str] = []
    original = qq_setup.github_latest_release

    def fake(*, api_url="", repo="", timeout=0.0):
        calls.append(api_url)
        return {"tag_name": "v1"}

    qq_setup.github_latest_release = fake
    try:
        release, base, _note = qq_setup.latest_release_with_mirrors(
            repo="r", api_url="http://127.0.0.1:1", timeout=0.1, log=lambda *_: None
        )
    finally:
        qq_setup.github_latest_release = original
    assert release == {"tag_name": "v1"} and calls == ["http://127.0.0.1:1"]


def test_download_with_mirrors_chain_order() -> None:
    d = _fresh("mirror_dl")
    calls: list[str] = []
    original = qq_setup.download_file

    def fake(url, dest, **kwargs):
        calls.append(url)
        if "gh-proxy.com" not in url:
            raise OSError("blocked")
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"ok")
        return Path(dest)

    qq_setup.download_file = fake
    try:
        gh = "https://github.com/o/r/releases/download/v1/a.zip"
        path, note = qq_setup.download_with_mirrors(
            gh, d / "a.zip",
            tuna_url="https://mirrors.tuna.tsinghua.edu.cn/github-release/o/r/v1/a.zip",
            timeout=0.1, log=lambda *_: None,
        )
    finally:
        qq_setup.download_file = original
    assert path is not None and path.read_bytes() == b"ok"
    assert calls[0].startswith("https://mirrors.tuna.tsinghua.edu.cn"), calls
    assert calls[1].startswith("https://gh-proxy.com/"), calls
    assert "gh-proxy.com" in note


def test_download_with_mirrors_non_github_direct() -> None:
    d = _fresh("mirror_direct")
    calls: list[str] = []
    original = qq_setup.download_file

    def fake(url, dest, **kwargs):
        calls.append(url)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x")
        return Path(dest)

    qq_setup.download_file = fake
    try:
        path, note = qq_setup.download_with_mirrors(
            "http://127.0.0.1:1/a.zip", d / "b.zip", timeout=0.1, log=lambda *_: None
        )
    finally:
        qq_setup.download_file = original
    assert path is not None and note == "直链" and calls == ["http://127.0.0.1:1/a.zip"]


def test_start_napcat_missing_and_launch() -> None:
    d = _fresh("start_napcat")
    ok, note = qq_setup.start_napcat(d)
    assert ok is False and "没找到启动脚本" in note, note

    (d / "napcat.bat").write_text("node.exe ./index.js", encoding="utf-8")
    calls: list = []
    original = qq_setup.subprocess.Popen
    qq_setup.subprocess.Popen = lambda args, **kwargs: calls.append((args, kwargs))
    try:
        ok2, note2 = qq_setup.start_napcat(d)
    finally:
        qq_setup.subprocess.Popen = original
    assert ok2 is True and "已启动 NapCat" in note2, note2
    assert calls and calls[0][0] == [str(d / "napcat.bat")], calls


def test_ensure_onebot_network_merge_backup_idempotent() -> None:
    d = _fresh("onebot_net")
    # 隔离：本机真实 napcat 目录可能已有配置，这里强制"找不到"
    original_find = qq_setup.find_config_files
    qq_setup.find_config_files = lambda *a, **k: []
    try:
        ok, note = qq_setup.ensure_onebot_network(d)
    finally:
        qq_setup.find_config_files = original_find
    assert ok is False and "没找到 OneBot 配置" in note, note

    cfg_dir = d / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "onebot11_1.json"
    cfg.write_text(json.dumps({
        "network": {"httpServers": [], "webSocketServers": [], "websocketClients": []}
    }, ensure_ascii=False), encoding="utf-8")

    ok2, note2 = qq_setup.ensure_onebot_network(d, token="tk", http_port=3100, ws_port=3101)
    assert ok2 is True and "已自动写入" in note2, note2
    parsed = qq_setup.parse_onebot_config(json.loads(cfg.read_text(encoding="utf-8")))
    assert parsed["http"]["port"] == 3100 and parsed["http"]["token"] == "tk"
    assert parsed["ws"]["port"] == 3101 and parsed["ws"]["token"] == "tk"
    backups = list(cfg_dir.glob("onebot11_1.json.bak_yinyue_*"))
    assert len(backups) == 1, backups

    ok3, note3 = qq_setup.ensure_onebot_network(d)
    assert ok3 is True and "已开启" in note3, note3
    assert len(list(cfg_dir.glob("onebot11_1.json.bak_yinyue_*"))) == 1


def test_napcat_running_detects_port() -> None:
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert qq_setup.napcat_running(ports=(port,), timeout=0.3) is True
    finally:
        server.close()
    assert qq_setup.napcat_running(ports=(port,), timeout=0.2) is False


def test_start_napcat_prefers_launcher() -> None:
    d = _fresh("start_launcher")
    inner = d / "napcat"
    inner.mkdir(parents=True, exist_ok=True)
    for name in ("launcher-user.bat", "launcher-win10-user.bat", "launcher.bat"):
        (inner / name).write_text("echo x", encoding="utf-8")
    (d / "napcat.bat").write_text("node.exe ./index.js", encoding="utf-8")
    calls: list = []
    original = qq_setup.subprocess.Popen
    qq_setup.subprocess.Popen = lambda args, **kwargs: calls.append((args, kwargs))
    try:
        ok, note = qq_setup.start_napcat(d)
    finally:
        qq_setup.subprocess.Popen = original
    assert ok is True and "launcher" in note, note
    used = Path(calls[0][0][0]).name
    assert used.startswith("launcher-") and used.endswith(".bat"), used
    assert calls[0][1]["cwd"] == str(inner), calls[0][1]


def test_find_qq_path_explicit_candidates() -> None:
    d = _fresh("qq_path")
    exe = d / "QQ.exe"
    exe.write_bytes(b"x")
    assert qq_setup._find_qq_path((exe,)) == exe
    other = qq_setup._find_qq_path((d / "nope.exe",))
    assert other is None or other.exists(), other   # 本机装了 QQ 时会回退找到真路径


def test_start_napcat_detached_env_and_flags() -> None:
    d = _fresh("napcat_detached")
    inner = d / "napcat"
    inner.mkdir(parents=True, exist_ok=True)
    for name in ("NapCatWinBootMain.exe", "NapCatWinBootHook.dll", "napcat.mjs", "qqnt.json"):
        (inner / name).write_bytes(b"x")
    qq = d / "QQ.exe"
    qq.write_bytes(b"x")
    calls: list = []
    original = qq_setup.subprocess.Popen
    qq_setup.subprocess.Popen = lambda args, **kwargs: calls.append((args, kwargs))
    try:
        ok, note = qq_setup.start_napcat_detached(d, qq_path=qq, open_webui=False)
    finally:
        qq_setup.subprocess.Popen = original
    assert ok is True and "无窗口" in note, note
    args, kwargs = calls[0]
    assert args == [str(inner / "NapCatWinBootMain.exe"), str(qq),
                    str(inner / "NapCatWinBootHook.dll")], args
    assert kwargs["cwd"] == str(inner)
    assert kwargs["creationflags"] == 0x08000000
    env = kwargs["env"]
    assert env["NAPCAT_INJECT_PATH"] == str(inner / "NapCatWinBootHook.dll")
    assert env["NAPCAT_MAIN_PATH"] == str(inner / "napcat.mjs").replace("\\", "/")
    assert (inner / "loadNapCat.js").read_text(encoding="utf-8").startswith("(async ()")


def test_stop_napcat_calls_taskkill() -> None:
    calls: list = []
    original_run = qq_setup.subprocess.run
    original_port_open = qq_setup._port_open
    qq_setup.subprocess.run = lambda args, **kwargs: calls.append(args)
    # 隔离外部状态：本机可能真在跑 NapCat（其 WebUI 端口在监听）。
    # stop_napcat 会轮询该端口来判断「是否真停成」，若不隔离，
    # 它会因为真实 NapCat 还开着而返回 False —— 测试就变成看环境脸色。
    # 本用例只关心 taskkill 的调用序列，故把端口探测固定为「已关闭」。
    qq_setup._port_open = lambda host, port, timeout=0.25: False
    try:
        ok, note = qq_setup.stop_napcat()
    finally:
        qq_setup.subprocess.run = original_run
        qq_setup._port_open = original_port_open
    # 顺序有语义：先杀常驻守护 NapCatWinBootMain，再杀 QQ.exe。
    # 反过来的话守护进程会把 QQ 重新拉起来（见 stop_napcat 的 docstring）。
    expected = [
        ["taskkill", "/F", "/IM", "NapCatWinBootMain.exe"],
        ["taskkill", "/F", "/IM", "QQ.exe"],
    ]
    assert ok is True, note
    assert [list(c[:4]) for c in calls] == expected, calls


def test_webui_target_reads_config() -> None:
    d = _fresh("webui_target")
    inner = d / "napcat"
    (inner / "config").mkdir(parents=True, exist_ok=True)
    (inner / "config" / "webui.json").write_text(
        json.dumps({"port": 6199, "token": "tk1"}), encoding="utf-8")
    url, port = qq_setup.webui_target(d)
    assert (url, port) == ("http://127.0.0.1:6199/webui?token=tk1", 6199), (url, port)
    url2, port2 = qq_setup.webui_target(d / "nope")
    assert (url2, port2) == ("http://127.0.0.1:6099/webui", 6099), (url2, port2)


def test_open_webui_when_ready_calls_opener() -> None:
    opened: list = []
    ok = qq_setup.open_webui_when_ready(
        "http://127.0.0.1:6099/webui", 6099, timeout=1.0, interval=0.01,
        opener=lambda url: opened.append(url) or True,
        port_check=lambda host, port: True,
    )
    assert ok is True and opened == ["http://127.0.0.1:6099/webui"], opened
    ok2 = qq_setup.open_webui_when_ready(
        "http://127.0.0.1:1/webui", 1, timeout=0.1, interval=0.01,
        opener=lambda url: opened.append(url) or True,
        port_check=lambda host, port: False,
    )
    assert ok2 is False and len(opened) == 1


def test_open_webui_when_ready_dir_rereads_token() -> None:
    d = _fresh("webui_reread")
    inner = d / "napcat"
    (inner / "config").mkdir(parents=True, exist_ok=True)
    opened: list = []
    state = {"first": True}

    def port_check(host, port):
        if state["first"]:  # 模拟"端口起来的同时 NapCat 才写 webui.json"
            state["first"] = False
            (inner / "config" / "webui.json").write_text(
                json.dumps({"port": 6199, "token": "newtk"}), encoding="utf-8")
        return True

    ok = qq_setup.open_webui_when_ready_dir(
        d, timeout=1.0, interval=0.01,
        opener=lambda url: opened.append(url) or True,
        port_check=port_check,
    )
    assert ok is True
    assert opened == ["http://127.0.0.1:6199/webui?token=newtk"], opened


def _rst_port(accepts: int) -> int:
    """开一个「接了就挂电话」的端口：读一点请求后带 SO_LINGER=0 关闭，逼出 RST。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(accepts)

    def serve() -> None:
        for _ in range(accepts):
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            try:
                conn.recv(4096)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            finally:
                conn.close()
        sock.close()

    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1]


def test_broken_handshake_is_not_a_missing_port() -> None:
    """「连上了但话被打断」不许被读成「这个端口上没有 OneBot」。"""
    port = _rst_port(1)
    hit = qq_setup.probe_login(f"http://127.0.0.1:{port}", token="")
    assert hit is not None, "连接被重置被当成「端口上没有 OneBot」了"
    assert hit.get("transport") is True, hit
    assert "打断" in str(hit.get("error")), hit


def test_broken_handshake_stops_the_scan() -> None:
    port = _rst_port(1)
    found = qq_setup.probe_http_ports((port,), token="")
    assert found is not None, "扫描把「打断」当成没有，继续往下找"
    assert "打断" in str(found.get("error")), found


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    try:
        for test in tests:
            try:
                test()
                print(f"  ok   {test.__name__}")
            except AssertionError as exc:
                failed += 1
                print(f"  FAIL {test.__name__}: {exc!r}")
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"qq setup checks: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
