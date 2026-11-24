"""配置收口（QQ 批次 4）：PLATFORMS_CONFIGS_JSON 合并进账号/主人映射。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config.settings import AppConfig, PlatformAccountSettings  # noqa: E402


def _cfg(configs_json: str) -> AppConfig:
    """构造一个只受通用配置影响的 AppConfig（隔离本机 legacy 槽）。"""
    cfg = AppConfig()
    cfg.platforms = PlatformAccountSettings(
        configs_json=configs_json,
        wechat_account="",
        wechat_owner_accounts="",
        qq_account="",
        qq_owner_accounts="",
        telegram_account="",
        telegram_owner_accounts="",
    )
    cfg.platform_configs = {}
    cfg.wechat.yinyue_wxid = ""
    cfg.wechat.owner_wxids = ""
    return cfg


def test_generic_configs_merge_into_maps() -> None:
    cfg = _cfg(
        '[{"platform":"qq","account_id":"10001","role":"yinyue"},'
        '{"platform":"qq","account_id":"90001","role":"owner"},'
        '{"platform":"telegram","account_id":"t-1","role":"yinyue"}]'
    )
    accounts = cfg.get_all_accounts()
    owners = cfg.get_all_owner_accounts()
    assert accounts.get("qq") == "10001", accounts
    assert accounts.get("telegram") == "t-1", accounts
    assert "90001" in owners.get("qq", []), owners
    assert "10001" not in owners.get("qq", []), owners
    assert "90001" not in set(accounts.values())


def test_legacy_slot_wins() -> None:
    cfg = _cfg('[{"platform":"qq","account_id":"10001","role":"yinyue"}]')
    cfg.platforms.qq_account = "legacy-777"
    cfg.platform_configs = {}
    assert cfg.get_all_accounts().get("qq") == "legacy-777"


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
    print(f"settings platform checks: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
