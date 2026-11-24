"""统一流程的通用件：.env 写入（备份 + 去重）、状态表渲染。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from scripts.onboarding.base import all_setups


def read_env(path: Path) -> dict[str, str]:
    """读 .env 为 {key: value}（后出现的键覆盖前面的，与运行期一致）。"""
    result: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return result
    for line in lines:
        parsed = _parse_line(line)
        if parsed:
            result[parsed[0]] = parsed[1]
    return result


def _parse_line(line: str) -> tuple[str, str] | None:
    text = str(line or "")
    if not text.strip() or text.lstrip().startswith("#") or "=" not in text:
        return None
    key, _, value = text.partition("=")
    key = key.strip()
    if not key:
        return None
    return key, value.strip()


def write_env(root: Path, values: dict[str, str], *, dry_run: bool = False,
              reason: str = "platform") -> dict[str, str]:
    """统一写 .env：先备份、同键去重（先删旧行再追加），返回每个键的动作。

    动作：``added`` / ``updated`` / ``unchanged``；全部 unchanged 时不备份、不写盘。
    """
    root = Path(root)
    env_path = root / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    existing: dict[str, str] = {}
    for line in lines:
        parsed = _parse_line(line)
        if parsed:
            existing[parsed[0]] = parsed[1]

    actions: dict[str, str] = {}
    out_lines = list(lines)
    for key, value in values.items():
        text = "" if value is None else str(value)
        if existing.get(key) == text:
            actions[key] = "unchanged"
            continue
        out_lines = [ln for ln in out_lines if (_parse_line(ln) or ("", ""))[0] != key]
        out_lines.append(f"{key}={text}")
        actions[key] = "updated" if key in existing else "added"

    if dry_run or not actions or all(v == "unchanged" for v in actions.values()):
        return actions
    if env_path.exists():
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = env_path.with_name(f".env.bak_{reason}_{stamp}")
        backup.write_bytes(env_path.read_bytes())
    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return actions


def short_status_line(setups: dict | None = None) -> str:
    """一行概览（启动器状态面板用）：wechat✓ qq✗ telegram✗ …

    ✓=凭据/连接齐；○=配了但还缺（如缺 token）；✗=未配置。
    """
    setups = setups if setups is not None else all_setups()
    parts = []
    for key in sorted(setups):
        try:
            st = setups[key].status()
        except Exception:  # noqa: BLE001
            st = {}
        if st.get("credentials_ok"):
            mark = "✓"
        elif st.get("configured"):
            mark = "○"
        else:
            mark = "✗"
        parts.append(f"{key}{mark}")
    return " ".join(parts)


def render_status_table(setups: dict | None = None) -> str:
    """统一的平台状态表（平台 / 状态 / 凭据 / 下一步）。"""
    setups = setups if setups is not None else all_setups()
    rows = [f"{'平台':<10}{'状态':<8}{'凭据':<6}下一步", "-" * 72]
    for key in sorted(setups):
        adapter = setups[key]
        try:
            st = adapter.status()
        except Exception as exc:  # noqa: BLE001
            st = {"configured": False, "credentials_ok": False,
                  "next_step": f"状态读取失败: {exc}"}
        state = "已配置" if st.get("configured") else "未配置"
        cred = "有效" if st.get("credentials_ok") else "-"
        rows.append(f"{key:<10}{state:<8}{cred:<6}{str(st.get('next_step') or '')[:46]}")
    return "\n".join(rows)
