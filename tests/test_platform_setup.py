"""平台接入统一流程（B1）：注册表 / 五步契约 / .env 写入 / 状态表 / 占位诚实性。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.onboarding import all_setups, get_platform_setup  # noqa: E402
from scripts.onboarding.base import STEP_KEYS, SetupContext, Step  # noqa: E402
from scripts.onboarding.common import render_status_table, write_env  # noqa: E402

PLATFORMS = ("wechat", "qq", "telegram", "discord", "bilibili", "douyin",
             "dingtalk", "feishu", "web")


def _tmp(name: str) -> Path:
    path = ROOT / f".tmp_onboarding_{name}"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_registry_has_all_platforms() -> None:
    setups = all_setups()
    missing = [key for key in PLATFORMS if key not in setups]
    assert not missing, missing
    assert get_platform_setup("QQ") is not None  # key 大小写不敏感


def test_five_steps_contract() -> None:
    for key, adapter in all_setups().items():
        steps = adapter.steps()
        assert [s.key for s in steps] == list(STEP_KEYS), (key, [s.key for s in steps])
        assert all(isinstance(s, Step) and s.title for s in steps), key


def test_status_table_renders_all() -> None:
    text = render_status_table()
    for key in all_setups():
        assert key in text, key
    assert "下一步" in text


def test_write_env_add_update_dedup_backup() -> None:
    root = _tmp("env")
    env = root / ".env"
    env.write_text("A=1\nB=2\nA=0\n", encoding="utf-8")
    actions = write_env(root, {"A": "9", "C": "3"}, reason="test")
    text = env.read_text(encoding="utf-8")
    assert actions == {"A": "updated", "C": "added"}, actions
    assert text.count("A=") == 1 and "A=9" in text and "C=3" in text, text
    assert len(list(root.glob(".env.bak_test_*"))) == 1
    # 全部 unchanged 时不写盘、不备份
    before = env.read_text(encoding="utf-8")
    actions2 = write_env(root, {"A": "9"}, reason="test")
    assert actions2 == {"A": "unchanged"}, actions2
    assert env.read_text(encoding="utf-8") == before
    assert len(list(root.glob(".env.bak_test_*"))) == 1
    shutil.rmtree(root, ignore_errors=True)


def test_placeholder_is_honest() -> None:
    adapter = get_platform_setup("telegram")
    status = adapter.status()
    assert status["configured"] is False
    assert "未实现" in status["next_step"]
    result = adapter.verify(SetupContext(root=ROOT))
    assert result.ok is False and "未实现" in result.detail


def test_qq_adapter_status_and_env_values() -> None:
    adapter = get_platform_setup("qq")
    status = adapter.status()
    assert {"configured", "credentials_ok", "next_step"} <= set(status)
    ctx = SetupContext(root=ROOT, options={
        "verify": {
            "login": {"user_id": "10001", "http_url": "http://127.0.0.1:3000"},
            "ws_url": "ws://127.0.0.1:3001",
            "token": "tok",
        },
        "owner": "90001",
    })
    values = adapter.env_values(ctx)
    assert values["PLATFORMS_QQ_ACCOUNT"] == "10001"
    assert values["PLATFORMS_QQ_OWNER_ACCOUNTS"] == "90001"
    assert values["QQ_WS_URL"] == "ws://127.0.0.1:3001"
    assert values["QQ_HTTP_URL"] == "http://127.0.0.1:3000"
    assert values["QQ_SEND_ENABLED"] == "true"


def test_qq_verify_with_fake_probes() -> None:
    adapter = get_platform_setup("qq")

    def fake_login(url: str, *, token: str = "", timeout: float = 3.0):
        return {"user_id": "10001", "nickname": "鲸鲸", "http_url": url}

    def fake_ws(url: str, *, timeout: float = 3.0) -> str:
        return "成功"

    ctx = SetupContext(root=ROOT, options={
        "http_url": "http://127.0.0.1:3000",
        "ws_url": "ws://127.0.0.1:3001",
        "token": "tok",
        "probe_login": fake_login,
        "probe_ws": fake_ws,
    })
    result = adapter.verify(ctx)
    assert result.ok, result.detail
    assert "10001" in result.detail
    assert ctx.options["verify"]["ws_url"] == "ws://127.0.0.1:3001"


def test_qq_prepare_uses_install_dir_and_hook() -> None:
    adapter = get_platform_setup("qq")
    seen: dict = {}
    started: list = []

    def fake_ensure(install_dir, *, auto_download=True, api_url="", asset_hint="",
                    proxy=""):
        seen.update(install_dir=str(install_dir), auto_download=auto_download,
                    api_url=api_url, asset_hint=asset_hint, proxy=proxy)
        return Path("/tmp/napcat"), "already"

    def fake_start(target):
        started.append(str(target))
        return True, "已启动 NapCat（测试）"

    ctx = SetupContext(root=ROOT, options={
        "install_dir": "D:/NapCat",
        "github_api": "http://mirror.example/api",
        "asset": "NapCat.Shell.zip",
        "ensure_napcat_install": fake_ensure,
        "napcat_running": lambda *a, **k: False,
        "start_napcat": fake_start,
    })
    result = adapter.prepare(ctx)
    assert result.ok, result.detail
    assert seen["install_dir"].endswith("NapCat"), seen
    assert seen["auto_download"] is True
    assert seen["api_url"] == "http://mirror.example/api"
    assert seen["asset_hint"] == "NapCat.Shell.zip"
    assert started == [str(Path("/tmp/napcat"))], started
    assert "已启动" in result.detail


def test_qq_prepare_skips_start_when_running() -> None:
    adapter = get_platform_setup("qq")
    started: list = []

    def fake_ensure(install_dir, *, auto_download=True, api_url="", asset_hint="",
                    proxy=""):
        return Path("/tmp/napcat"), "already"

    ctx = SetupContext(root=ROOT, options={
        "ensure_napcat_install": fake_ensure,
        "napcat_running": lambda *a, **k: True,
        "start_napcat": lambda target: (started.append(str(target)), (True, "x"))[1],
    })
    result = adapter.prepare(ctx)
    assert result.ok and "已在运行" in result.detail, result.detail
    assert started == []


def test_short_status_line_marks() -> None:
    from scripts.onboarding.common import short_status_line

    text = short_status_line()
    assert "wechat" in text and "qq" in text, text
    assert any(mark in text for mark in ("✓", "○", "✗")), text


def test_qq_prepare_fails_fast_without_qq() -> None:
    adapter = get_platform_setup("qq")

    def fake_ensure(install_dir, *, auto_download=True, api_url="", asset_hint="",
                    proxy=""):
        return Path("/tmp/napcat"), "downloaded:v9:x.zip"

    ctx = SetupContext(root=ROOT, options={
        "ensure_napcat_install": fake_ensure,
        "napcat_running": lambda *a, **k: False,
        "start_napcat": lambda target: (False, "没找到 QQ.exe（注册表与常见路径都没有）：先装 QQ"),
    })
    result = adapter.prepare(ctx)
    assert result.ok is False and "QQ" in result.next_action, result.next_action


def test_wechat_authorize_modes() -> None:
    adapter = get_platform_setup("wechat")

    dry = adapter.authorize(SetupContext(root=ROOT, dry_run=True))
    assert dry.ok is True and "start.py" in dry.detail, dry.detail

    manual = adapter.authorize(SetupContext(root=ROOT, options={"no_elevate": True}))
    assert manual.ok is True and "需要人工" in manual.detail, manual.detail

    launched: list = []
    ok = adapter.authorize(SetupContext(
        root=ROOT,
        options={"launch_start_py": lambda root: (launched.append(root), True)[1]},
    ))
    assert ok.ok is True and "已弹出管理员窗口" in ok.detail, ok.detail
    assert launched == [ROOT], launched

    failed = adapter.authorize(SetupContext(
        root=ROOT, options={"launch_start_py": lambda root: False}))
    assert failed.ok is False and "手动" in failed.next_action, failed.next_action


def test_launch_first_run_passes_extra_args() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import setup_wizard

    calls: list = []
    original = setup_wizard.subprocess.Popen
    setup_wizard.subprocess.Popen = lambda args, **kwargs: calls.append(args)
    try:
        ok = setup_wizard.launch_first_run(ROOT, "--no-scheduler")
    finally:
        setup_wizard.subprocess.Popen = original
    assert ok is True and calls, calls
    assert "--no-scheduler" in " ".join(calls[0]), calls


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {test.__name__}: {exc!r}")
    print(f"platform setup checks: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
