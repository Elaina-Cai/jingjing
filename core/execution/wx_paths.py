"""Single owner of every on-disk location the WeChat execution layer touches.

Before this module existed the same path arithmetic was written out three times:
once in wx_sticker_sender, once in sticker_auto_setup, and once more by hand
inside web/routes_api.  Three copies of a rule that has to agree is a latent
bug -- a sticker written under one convention is invisible to the others.

The on-disk layout is unchanged (the same data/wechat/... tree); only the
computation moved here.

Layout
------

    data/wechat/                              platform root
        sticker_index.json                    moods for the default account
        sticker_templates/                    source images, default account
        ui_templates/                         learned button/tab crops
        diagnose/                             annotated debug frames from the CLI
        <account_id>/                         one WeChat account
            sticker_index.json
            sticker_templates/
            sticker_thumbs/                   crops of the popup itself
            match_debug/

Note on data isolation: core/config/settings.py has a separate, config-driven
path scheme (paths.platforms_dir) for account databases and media.  The sticker
tree deliberately stays where it is so that existing collections keep working;
reconciling the two schemes is a separate change.
"""

from __future__ import annotations

from pathlib import Path

import structlog

log = structlog.get_logger()

__all__ = [
    "DATA_ROOT", "DEFAULT_PLATFORM",
    "platform_dir", "account_dir", "sticker_index_path", "sticker_templates_dir",
    "sticker_thumbs_dir", "ui_templates_dir", "diagnose_dir", "match_debug_dir",
]

DEFAULT_PLATFORM = "wechat"


def _resolve_data_root() -> Path:
    """Locate the data directory.

    The normal case is anchored to the repository root rather than the working
    directory, so that importing this package from anywhere (tests, tooling, a
    scheduler started with a different cwd) reads and writes the same tree.
    The working-directory form is kept as a fallback for unusual deployments.
    """
    anchored = Path(__file__).resolve().parents[2] / "data"
    if anchored.exists():
        return anchored

    cwd_relative = Path("data")
    if cwd_relative.exists():
        log.warning("data_root_from_cwd", resolved=str(cwd_relative.resolve()),
                    expected=str(anchored))
        return cwd_relative

    return anchored


DATA_ROOT = _resolve_data_root()


def platform_dir(platform: str = DEFAULT_PLATFORM) -> Path:
    """Root of one platform's sticker data."""
    return DATA_ROOT / platform


def account_dir(platform: str = DEFAULT_PLATFORM, account_id: str = "") -> Path:
    """Root of one account's sticker data (the platform root when anonymous)."""
    if account_id:
        return DATA_ROOT / platform / account_id
    return DATA_ROOT / platform


# Sticker locations now follow the per-account isolation layout and are owned by
# db.sticker_store, so that every consumer resolves them the same way.  Imported
# lazily: db pulls in the ORM, which reaches back into core.execution, and a
# module-level import here would close that loop.
def _sticker_store(platform: str, account_id: str):
    from db import sticker_store

    return sticker_store.StickerStore(platform, account_id)


def sticker_index_path(platform: str = DEFAULT_PLATFORM, account_id: str = "") -> Path:
    return _sticker_store(platform, account_id).registry_path


def sticker_templates_dir(platform: str = DEFAULT_PLATFORM, account_id: str = "") -> Path:
    return _sticker_store(platform, account_id).templates_dir


def sticker_thumbs_dir(platform: str = DEFAULT_PLATFORM, account_id: str = "") -> Path:
    """Crops of the popup itself, learned by capture_panel_thumbs()."""
    return _sticker_store(platform, account_id).thumbs_dir


def ui_templates_dir(platform: str = DEFAULT_PLATFORM) -> Path:
    """Learned button and tab crops (legacy calibration artefacts)."""
    return platform_dir(platform) / "ui_templates"


def diagnose_dir(platform: str = DEFAULT_PLATFORM) -> Path:
    """Annotated frames written by the diagnostics CLI."""
    return platform_dir(platform) / "diagnose"


def match_debug_dir(platform: str = DEFAULT_PLATFORM, account_id: str = "") -> Path:
    return account_dir(platform, account_id) / "match_debug"
